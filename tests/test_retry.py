"""Generation recovery through the API; fake paid providers and real local assembly."""
import asyncio
import copy
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main, media


VIDEO_ID = "a" * 32
EDIT_ID = "b" * 32


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path)
    monkeypatch.setattr(main, "JOBS", {})
    monkeypatch.setattr(main, "TASKS", set())
    monkeypatch.setattr(main, "VIDEO_LOCKS", {})
    monkeypatch.setattr(main, "WORKERS", asyncio.Semaphore(2))
    config = copy.deepcopy(main.CONFIG)
    config["keys"] = {name: "test-only-not-a-real-key" for name in config["keys"]}
    monkeypatch.setattr(main, "CONFIG", config)
    folder = tmp_path / VIDEO_ID / "edits" / EDIT_ID
    folder.mkdir(parents=True)
    main.write_json(tmp_path / VIDEO_ID / "video.json", {
        "id": VIDEO_ID, "name": "Retry fixture", "duration": 4.0,
        "width": 160, "height": 96, "fps": 24.0, "has_audio": False, "demo": False,
    })
    main.write_json(folder / "edit.json", {
        "id": EDIT_ID, "video_id": VIDEO_ID, "instruction": "Make the background blue",
        "prompt": "Change only the background to blue", "start": 1.0, "end": 3.0,
        "status": "planned", "demo": False,
    })
    for name in ("start", "end"):
        Image.new("RGB", (160, 96), "red").save(folder / f"{name}.jpg")
    with TestClient(main.app) as connection:
        yield connection


def wait_job(client, response):
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.02)
    pytest.fail("The isolated generation job did not finish")


def make_clip(path, color, duration):
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        f"color=c={color}:s=160x96:r=24:d={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True, capture_output=True, timeout=20)


@pytest.mark.skipif(not media.available(), reason="FFmpeg and ffprobe required")
def test_retry_after_stitch_failure_reuses_all_paid_outputs(client, monkeypatch, tmp_path):
    source = main.DATA / VIDEO_ID / "video.mp4"
    replacement_fixture = tmp_path / "generated-fixture.mp4"
    make_clip(source, "red", 4)
    make_clip(replacement_fixture, "blue", 1)
    calls = {"images": 0, "video": 0, "stitch": 0}
    real_stitch = media.stitch

    async def fake_edit_image(config, source_path, output_path, instruction, emit, label):
        calls["images"] += 1
        # Different model canvas also exercises normalization before conditioning.
        Image.new("RGB", (256, 256), "blue").save(output_path)
        await emit("test-image", "test", "Fake image provider", label, "completed")

    async def fake_generate_video(config, first, last, output, prompt, duration, emit):
        calls["video"] += 1
        for anchor in (first, last):
            with Image.open(anchor) as image:
                assert image.size == (160, 96)
        shutil.copyfile(replacement_fixture, output)
        await emit("test-video", "test", "Fake video provider", "Generated test clip", "completed")

    def fail_first_stitch(*args, **kwargs):
        calls["stitch"] += 1
        if calls["stitch"] == 1:
            raise media.MediaError("Simulated temporary local assembly failure")
        return real_stitch(*args, **kwargs)

    monkeypatch.setattr(main.providers, "edit_image", fake_edit_image)
    monkeypatch.setattr(main.providers, "generate_video", fake_generate_video)
    monkeypatch.setattr(media, "stitch", fail_first_stitch)
    endpoint = f"/api/videos/{VIDEO_ID}/edits/{EDIT_ID}/generate"

    first = wait_job(client, client.post(endpoint))
    assert first["status"] == "failed"
    assert "assembly failure" in first["error"]
    assert calls == {"images": 2, "video": 1, "stitch": 1}
    assert main.load_edit(VIDEO_ID, EDIT_ID)["status"] == "failed"
    folder = main.edit_folder(VIDEO_ID, EDIT_ID)
    assert (folder / "edited-start.png").is_file()
    assert (folder / "edited-end.png").is_file()
    assert media.probe(folder / "replacement.mp4")["duration"] == pytest.approx(1)

    retried = wait_job(client, client.post(endpoint))
    assert retried["status"] == "completed", retried
    assert calls == {"images": 2, "video": 1, "stitch": 2}
    edit = retried["result"]["edit"]
    assert edit["status"] == "completed" and "error" not in edit
    assert main.load_edit(VIDEO_ID, EDIT_ID)["status"] == "completed"
    assert sum(event["stage"] == "reuse-anchor" for event in retried["events"]) == 2
    assert any(event["stage"] == "reuse-replacement" for event in retried["events"])
    assert media.probe(folder / "output.mp4")["duration"] == pytest.approx(4, abs=1 / media.FPS)
    response = client.get(edit["output_url"], headers={"Range": "bytes=0-31"})
    assert response.status_code == 206 and len(response.content) == 32

    reopened = wait_job(client, client.post(endpoint))
    assert reopened["status"] == "completed"
    assert calls == {"images": 2, "video": 1, "stitch": 2}


@pytest.mark.parametrize("has_previous_job_id", [False, True])
def test_orphaned_generation_recovers_while_unrelated_job_is_running(client, has_previous_job_id):
    edit = main.load_edit(VIDEO_ID, EDIT_ID)
    edit["status"] = "generating"
    if has_previous_job_id:
        edit["job_id"] = "lost-job-from-previous-process"
    main.write_json(main.edit_folder(VIDEO_ID, EDIT_ID) / "edit.json", edit)
    main.JOBS["unrelated-job"] = {"id": "unrelated-job", "status": "running", "type": "analyze"}

    response = client.get(f"/api/videos/{VIDEO_ID}/edits")
    assert response.status_code == 200
    recovered = response.json()["edits"][0]
    assert recovered["status"] == "failed"
    assert "restart" in recovered["error"]
    assert main.load_edit(VIDEO_ID, EDIT_ID)["status"] == "failed"
    assert main.JOBS["unrelated-job"]["status"] == "running"

    # The same recovery read must not fail a generation with its own active job.
    recovered.update(status="generating", job_id="own-live-job")
    main.write_json(main.edit_folder(VIDEO_ID, EDIT_ID) / "edit.json", recovered)
    main.JOBS["own-live-job"] = {"id": "own-live-job", "status": "running", "type": "generate"}
    current = client.get(f"/api/videos/{VIDEO_ID}/edits").json()["edits"][0]
    assert current["status"] == "generating"
    assert client.post(f"/api/videos/{VIDEO_ID}/edits/{EDIT_ID}/generate").status_code == 409
