"""Offline tests for one-key fal reasoning, evidence and source editing."""
import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from app import fal_models as f
from app import source_editor
from app.providers import ProviderError

REAL_CLIENT = httpx.AsyncClient
CONFIG = {"keys": {"fal": "test-fal-secret"}, "models": {"fal_video_edit_model": f.SOURCE_MODEL}}
VIDEO = b"mock-video-payload-for-offline-tests-only"
STATUS_URL = "https://queue.fal.run/status"
RESULT_URL = "https://queue.fal.run/result"
VIDEO_URL = "https://v3.fal.media/files/result.mp4"


class FalModelsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source.mp4"
        self.source.write_bytes(VIDEO)
        self.output = self.folder / "result.mp4"
        self.frame = self.folder / "frame.jpg"
        Image.new("RGB", (400, 720), "yellow").save(self.frame)
        self.config = copy.deepcopy(CONFIG)
        self.calls, self.events = [], []
        validation = patch.object(source_editor, "_validate_source")
        validation.start()
        self.addCleanup(validation.stop)

    def transport(self, handler):
        transport = httpx.MockTransport(handler)
        return patch.object(f.httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs))

    async def emit(self, stage, provider, model, message, status="running", artifacts=None):
        self.events.append({"stage": stage, "provider": provider, "model": model, "message": message, "status": status})

    async def edit(self, **kwargs):
        await f.edit_source_video(self.config, self.source, self.output, kwargs.get("prompt", "Place the can on a beach."), kwargs.get("duration", 5.1667), self.emit)

    def ticket(self):
        return json.loads(self.output.with_suffix(".fal-source-job.json").read_text())

    def generation_handler(self, request):
        self.calls.append(request)
        if request.method == "POST":
            self.assertEqual(str(request.url), f"https://queue.fal.run/{f.SOURCE_MODEL}")
            self.assertEqual(request.headers["authorization"], "Key test-fal-secret")
            body = json.loads(request.content)
            self.assertEqual(body["task"], "editing")
            self.assertIn("@Video1", body["prompt"])
            self.assertEqual(base64.b64decode(body["video_urls"][0].split(",", 1)[1]), VIDEO)
            self.assertEqual(body["resolution"], "720p")
            self.assertEqual(body["codec"], "H264")
            self.assertFalse(body["generate_audio"])
            self.assertNotIn("duration", body)
            self.assertNotIn("aspect_ratio", body)
            return httpx.Response(200, json={"request_id": "saved-request", "status_url": STATUS_URL, "response_url": RESULT_URL})
        if str(request.url) == STATUS_URL:
            self.assertEqual(request.headers["authorization"], "Key test-fal-secret")
            return httpx.Response(200, json={"status": "COMPLETED"})
        if str(request.url) == RESULT_URL:
            return httpx.Response(200, json={"video": {"url": VIDEO_URL}})
        self.assertEqual(str(request.url), VIDEO_URL)
        self.assertNotIn("authorization", request.headers)
        return httpx.Response(200, content=VIDEO)

    async def test_router_selects_text_images_and_video_with_grounded_timestamps(self):
        def handler(request):
            self.calls.append(request)
            self.assertEqual(request.headers["authorization"], "Key test-fal-secret")
            return httpx.Response(200, json={"output": '{"answer":"the can"}'})
        with self.transport(handler):
            self.assertEqual(await f.call_json(self.config, f.DEFAULT_CHAT_MODEL, "Answer."), {"answer": "the can"})
            await f.call_json(self.config, f.DEFAULT_VISION_MODEL, "Inspect.", frames=[{"timestamp": 4, "path": self.frame}, {"timestamp": 1.25, "path": self.frame}])
            await f.call_json(self.config, f.DEFAULT_VISION_MODEL, "Analyze.", video_path=self.source)
        self.assertEqual([request.url.path for request in self.calls], ["/openrouter/router", "/openrouter/router/vision", "/openrouter/router/video"])
        images = json.loads(self.calls[1].content)
        self.assertEqual(len(images["image_urls"]), 2)
        self.assertIn("Image 1: 1.250 seconds; Image 2: 4.000 seconds", images["prompt"])
        self.assertIn("seconds", images["system_prompt"])
        video = json.loads(self.calls[2].content)
        self.assertTrue(video["video_urls"][0].startswith("data:video/mp4;base64,"))
        self.assertEqual(video["model"], f.DEFAULT_VISION_MODEL)

    async def test_router_errors_never_expose_secret_provider_bodies(self):
        for response in [httpx.Response(401, text="test-fal-secret"), httpx.Response(200, json={"partial": True, "output": '{"answer":"incomplete"}'}), httpx.Response(200, json={"error": "test-fal-secret"})]:
            with self.subTest(status=response.status_code), self.transport(lambda request: response):
                with self.assertRaises(ProviderError) as caught:
                    await f.call_json(self.config, f.DEFAULT_CHAT_MODEL, "Answer.")
                self.assertNotIn("test-fal-secret", str(caught.exception))

    async def test_router_limits_are_checked_before_network(self):
        with self.transport(lambda request: self.fail("No request expected")):
            with self.assertRaises(ProviderError):
                await f.call_json(self.config, "invalid model", "Inspect.")
            with self.assertRaises(ProviderError):
                await f.call_json(self.config, f.DEFAULT_VISION_MODEL, "Inspect.", frames=[{"timestamp": 1, "path": self.frame}], video_path=self.source)
            with self.source.open("wb") as target:
                target.truncate(f.MAX_ANALYSIS_BYTES + 1)
            with self.assertRaisesRegex(ProviderError, "14 MB"):
                await f.call_json(self.config, f.DEFAULT_VISION_MODEL, "Inspect.", video_path=self.source)

    async def test_source_edit_contract_and_saved_ticket(self):
        with self.transport(self.generation_handler):
            await self.edit()
        self.assertEqual(self.output.read_bytes(), VIDEO)
        self.assertEqual(self.ticket()["state"], "completed")
        self.assertEqual(len(self.calls), 4)
        self.assertNotIn("test-fal-secret", json.dumps(self.ticket()))
        self.assertNotIn("base64", json.dumps(self.ticket()))
        self.assertEqual(self.output.with_suffix(".fal-source-job.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.events[-1]["status"], "completed")

    async def test_queue_real_states_and_download_recovery_do_not_resubmit(self):
        phases, failures = iter(["IN_QUEUE", "IN_PROGRESS", "COMPLETED"]), [True]
        def handler(request):
            if str(request.url) == STATUS_URL:
                self.calls.append(request)
                return httpx.Response(200, json={"status": next(phases)})
            if str(request.url) == VIDEO_URL and failures:
                failures.pop()
                return httpx.Response(503)
            return self.generation_handler(request)
        with self.transport(handler), patch.object(f, "POLL_SECONDS", 0):
            with self.assertRaises(ProviderError):
                await self.edit()
            self.assertEqual(self.ticket()["state"], "completed")
            await self.edit()
        self.assertEqual(sum(request.method == "POST" for request in self.calls), 1)
        self.assertEqual(sum(str(request.url) == STATUS_URL for request in self.calls), 3)
        self.assertTrue(any(event["stage"] == "reuse-source-upload" for event in self.events))
        self.assertEqual(self.output.read_bytes(), VIDEO)

    async def test_ambiguous_submission_blocks_blind_resubmission(self):
        def handler(request):
            self.calls.append(request)
            raise httpx.ReadTimeout("test-fal-secret", request=request)
        with self.transport(handler):
            with self.assertRaises(ProviderError) as caught:
                await self.edit()
            self.assertNotIn("test-fal-secret", str(caught.exception))
            self.assertEqual(self.ticket()["state"], "submitting")
            with self.assertRaises(ProviderError):
                await self.edit()
        self.assertEqual(len(self.calls), 1)

    async def test_terminal_failure_and_changed_input_cannot_resubmit(self):
        def handler(request):
            if str(request.url) == STATUS_URL:
                return httpx.Response(200, json={"status": "COMPLETED", "error": "test-fal-secret"})
            return self.generation_handler(request)
        with self.transport(handler):
            with self.assertRaises(ProviderError):
                await self.edit()
            self.assertEqual(self.ticket()["state"], "terminal_failed")
            with self.assertRaisesRegex(ProviderError, "fresh generation"):
                await self.edit()
            with self.assertRaisesRegex(ProviderError, "different inputs"):
                await self.edit(prompt="Different request.")
        self.assertEqual(sum(request.method == "POST" for request in self.calls), 1)
        self.assertNotIn("test-fal-secret", json.dumps(self.events))

    async def test_output_redirect_cannot_reach_private_or_untrusted_host(self):
        def handler(request):
            if str(request.url) == VIDEO_URL:
                return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/api/config"})
            return self.generation_handler(request)
        with self.transport(handler):
            with self.assertRaisesRegex(ProviderError, "unsupported video download"):
                await self.edit()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix(".mp4.part").exists())
        for url in ["https://fal.media.attacker.com/out.mp4", "https://user:password@fal.media/out.mp4", "https://storage.googleapis.com/other-bucket/out.mp4"]:
            with self.assertRaises(ProviderError):
                f._download_url(url)

    async def test_source_limits_and_duration_boundary_rounding(self):
        for duration in [3.9, 30.1, float("nan"), True]:
            with self.subTest(duration=duration), self.transport(lambda request: self.fail("No request expected")):
                with self.assertRaises(ProviderError):
                    await self.edit(duration=duration)
        with self.transport(self.generation_handler):
            await self.edit(duration=3.9999999999999996)
        self.output.with_suffix(".fal-source-job.json").unlink()
        with self.source.open("wb") as target:
            target.truncate(f.MAX_SOURCE_BYTES + 1)
        with self.transport(lambda request: self.fail("Oversize video must not upload")):
            with self.assertRaisesRegex(ProviderError, "64 MB"):
                await self.edit()


if __name__ == "__main__":
    unittest.main()
