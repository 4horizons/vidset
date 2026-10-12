"""Offline checks for real job timing and preserved workflow provenance."""
import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import main
from app.providers import ProviderError


WORKFLOW = {"title": "Edit video", "steps": [{"id": "edit", "title": "Edit", "provider": "test",
              "model": "fake-model", "stages": ["edit"], "complete_on": ["edit"]}]}
VIDEO_ID, EDIT_ID = "d" * 32, "e" * 32


class JobProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name, value in (("JOBS", {}), ("TASKS", set()), ("VIDEO_LOCKS", {}),
                            ("WORKERS", asyncio.Semaphore(2)), ("DATA", self.folder)):
            patcher = patch.object(main, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def finish(self, submitted):
        tasks = list(main.TASKS)
        if tasks:
            await asyncio.gather(*tasks)
        return main.JOBS[submitted["job_id"]]

    async def test_queued_manifest_and_actual_start_and_completion_times(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def worker(emit):
            entered.set()
            await release.wait()
            return {"answer": "done"}

        manifest = copy.deepcopy(WORKFLOW)
        submitted = main.start_job("generate", worker, workflow=manifest)
        job = main.JOBS[submitted["job_id"]]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["workflow"], WORKFLOW)
        self.assertTrue(job["created_at"])
        self.assertIsNone(job["started_at"])
        self.assertIsNone(job["completed_at"])
        manifest["steps"].clear()
        self.assertEqual(job["workflow"], WORKFLOW)  # dispatch owns its snapshot
        await entered.wait()
        self.assertEqual(job["status"], "running")
        self.assertIsNotNone(job["started_at"])
        self.assertIsNone(job["completed_at"])
        release.set()
        job = await self.finish(submitted)
        self.assertEqual(job["status"], "completed")
        dates = [datetime.fromisoformat(job[key]) for key in ("created_at", "started_at", "completed_at")]
        self.assertTrue(all(date.tzinfo is not None for date in dates))
        self.assertLessEqual(dates[0], dates[1])
        self.assertLessEqual(dates[1], dates[2])

    async def test_informational_events_do_not_stop_the_stage_clock(self):
        clock = [10.0]

        async def worker(emit):
            await emit("edit", "test", "fake-model", "started")
            clock[0] = 12.0
            await emit("edit", "test", "fake-model", "queued", "info")
            clock[0] = 14.0
            await emit("edit", "test", "fake-model", "progress", "info")
            clock[0] = 17.5
            await emit("edit", "test", "fake-model", "finished", "completed")
            return {}

        with patch.object(main, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            job = await self.finish(main.start_job("generate", worker, workflow=WORKFLOW))
        self.assertEqual(job["status"], "completed")
        self.assertTrue(all("elapsed_ms" not in event for event in job["events"][:-1]))
        self.assertEqual(job["events"][-1]["elapsed_ms"], 7500)

    async def test_failed_stage_retains_elapsed_time_and_terminal_job_status(self):
        clock = [20.0]

        async def worker(emit):
            await emit("edit", "test", "fake-model", "started")
            clock[0] = 20.625
            await emit("edit", "test", "fake-model", "provider unavailable", "failed")
            raise ProviderError("Provider unavailable; retry the saved request.")

        with patch.object(main, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            job = await self.finish(main.start_job("generate", worker, workflow=WORKFLOW))
        self.assertEqual(job["status"], "failed")
        self.assertIsNotNone(job["completed_at"])
        self.assertEqual(job["events"][1]["elapsed_ms"], 625)
        self.assertEqual(job["events"][1]["status"], "failed")
        self.assertEqual(job["events"][-1]["stage"], "error")
        self.assertEqual(job["events"][-1]["status"], "failed")
        self.assertIsNone(job["result"])

    async def test_completed_edit_persists_original_run_and_reopen_keeps_it(self):
        edit_folder = self.folder / VIDEO_ID / "edits" / EDIT_ID
        edit_folder.mkdir(parents=True)
        main.write_json(self.folder / VIDEO_ID / "video.json", {"id": VIDEO_ID, "demo": False, "duration": 5})
        edit = {"id": EDIT_ID, "video_id": VIDEO_ID, "status": "completed", "demo": False, "strategy": "source_video"}

        async def worker(emit):
            await emit("edit", "test", "fake-model", "started")
            await emit("edit", "test", "fake-model", "finished", "completed")
            return {"edit": edit}

        original = await self.finish(main.start_job("generate", worker, VIDEO_ID, WORKFLOW))
        saved_path = edit_folder / "edit.json"
        original_bytes = saved_path.read_bytes()
        saved = json.loads(original_bytes)
        self.assertEqual(saved["workflow"], WORKFLOW)
        self.assertEqual(saved["run_events"], original["events"])
        self.assertEqual(saved["run_started_at"], original["started_at"])
        self.assertEqual(saved["run_completed_at"], original["completed_at"])
        self.assertEqual(saved["job_id"], original["id"])

        # Calling this endpoint for a completed edit takes its saved-result path;
        # no provider, media processing, or real API call is needed.
        reopened = await self.finish(await main.generate_endpoint(VIDEO_ID, EDIT_ID))
        self.assertEqual(reopened["status"], "completed")
        self.assertTrue(reopened["result"]["reused"])
        self.assertEqual([row["id"] for row in reopened["workflow"]["steps"]], ["output"])
        self.assertEqual(saved_path.read_bytes(), original_bytes)
        self.assertEqual(reopened["result"]["edit"]["job_id"], original["id"])


if __name__ == "__main__":
    unittest.main()
