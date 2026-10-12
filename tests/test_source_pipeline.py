"""Offline source-edit API flows, using real FFmpeg and explicitly fake cloud work."""
import asyncio
import copy
import io
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main, media, source_editor


pytestmark = pytest.mark.skipif(not media.available(), reason="FFmpeg and ffprobe required")
VIDEO_ID = "c" * 32


def ffmpeg(*args):
    return subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)],
                          check=True, capture_output=True, timeout=30).stdout


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    root = tmp_path_factory.mktemp("source-edit-fixtures")
    source, generated = root / "original.mp4", root / "fake-generated.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=320x320:r=24:d=34",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=34",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source)
    # Shorter than the maximum selection: full assembly must fit its duration.
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=320x320:r=24:d=4",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", generated)
    return source, generated


@pytest.fixture
def client(tmp_path, monkeypatch, clips):
    monkeypatch.setattr(main, "DATA", tmp_path)
    monkeypatch.setattr(main, "JOBS", {})
    monkeypatch.setattr(main, "TASKS", set())
    monkeypatch.setattr(main, "VIDEO_LOCKS", {})
    monkeypatch.setattr(main, "WORKERS", asyncio.Semaphore(2))
    config = copy.deepcopy(main.CONFIG)
    config["keys"] = {name: "" for name in config["keys"]}
    config["keys"].update(gemini="fake-planner-key", higgsfield="fake-id:fake-secret")
    config["models"].update(vision_provider="gemini", vision_model="gemini-2.5-flash",
                            chat_provider="gemini", chat_model="gemini-2.5-flash",
                            video_edit_model="bytedance/seedance-2.5/video-edit")
    monkeypatch.setattr(main, "CONFIG", config)
    folder = tmp_path / VIDEO_ID
    folder.mkdir()
    shutil.copyfile(clips[0], folder / "video.mp4")
    main.write_json(folder / "video.json", {
        "id": VIDEO_ID, "name": "Source editing test", **media.probe(folder / "video.mp4"),
        "demo": False, "analysis": {"summary": "A red scene.", "events": [
            {"start": 0, "end": 34, "label": "Red background", "description": "Background remains red."}]},
    })

    async def unexpected_provider_call(*args, **kwargs):
        pytest.fail("No actual cloud call or anchor-image generation is allowed in this test")

    monkeypatch.setattr(main.providers, "_call_json", unexpected_provider_call)
    monkeypatch.setattr(main.providers, "edit_image", unexpected_provider_call)
    monkeypatch.setattr(main.providers, "generate_video", unexpected_provider_call)
    monkeypatch.setattr(source_editor, "edit_source_video", unexpected_provider_call)
    with TestClient(main.app) as connection:
        yield connection


def wait_job(client, response):
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.02)
    pytest.fail("The isolated source-edit job did not finish")


def install_fake_planner(monkeypatch, calls):
    async def fake_stage(config, provider, model, prompt, emit, stage, message, **kwargs):
        calls.append(prompt)
        assert "4-30 seconds" in prompt
        assert "actual source video segment" in prompt
        assert "original audio is restored locally" in prompt
        assert "NOT the source motion" not in prompt
        await emit(stage, provider, model, "Offline fake planning response", "completed")
        # Deliberately different: explicit user timestamps must win.
        return {"start": 10, "end": 14, "rationale": "Replace only the requested background",
                "prompt": "Make the red background blue; preserve subject motion, reveal, geometry and camera."}
    monkeypatch.setattr(main.providers, "_stage_call", fake_stage)


def plan(client, start, end):
    return wait_job(client, client.post(f"/api/videos/{VIDEO_ID}/edits/plan", json={
        "strategy": "source_video", "instruction": "Make the red background blue", "start": start, "end": end,
    }))


@pytest.mark.parametrize("start,end", [(2, 6), (2, 32)])
def test_source_plan_accepts_both_duration_limits_and_keeps_manual_times(client, monkeypatch, start, end):
    calls = []
    install_fake_planner(monkeypatch, calls)
    result = plan(client, start, end)
    assert result["status"] == "completed", result
    edit = result["result"]["edit"]
    assert edit["strategy"] == "source_video"
    assert (edit["start"], edit["end"]) == (start, end)
    assert len(calls) == 1
    assert edit["anchors"]["start"]["timestamp"] == start
    assert edit["anchors"]["end"]["timestamp"] == pytest.approx(end - 1 / media.FPS, abs=0.001)


@pytest.mark.parametrize("start,end", [(0, 3.9), (0, 30.1), (4, 35), (6, 2)])
def test_source_plan_rejects_outside_limits_before_model_call(client, monkeypatch, start, end):
    async def forbidden(*args, **kwargs):
        pytest.fail("Range validation must happen before spending model tokens")
    monkeypatch.setattr(main.providers, "_stage_call", forbidden)
    response = client.post(f"/api/videos/{VIDEO_ID}/edits/plan", json={
        "strategy": "source_video", "instruction": "Make background blue", "start": start, "end": end,
    })
    assert response.status_code == 400
    assert main.JOBS == {}


def decoded_audio(path):
    return ffmpeg("-i", path, "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1")


def pixel_at(path, timestamp):
    raw = ffmpeg("-ss", timestamp, "-i", path, "-frames:v", "1",
                 "-vf", "crop=2:2:160:160", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1")
    return tuple(raw[:3])


@pytest.mark.parametrize("selected_seconds,fail_first_assembly", [(4, False), (30, True)])
def test_source_generation_preserves_audio_evidence_and_reuses_paid_result(
        client, monkeypatch, clips, selected_seconds, fail_first_assembly):
    planning_calls = []
    install_fake_planner(monkeypatch, planning_calls)
    planned = plan(client, 2, 2 + selected_seconds)
    assert planned["status"] == "completed", planned
    edit = planned["result"]["edit"]
    # Planning already finished. Source generation requires only Higgsfield.
    main.CONFIG["keys"] = {name: "" for name in main.CONFIG["keys"]}
    main.CONFIG["keys"]["higgsfield"] = "fake-id:fake-secret"
    calls = {"source_editor": 0, "stitch": 0}
    actual_stitch = media.stitch

    async def fake_source_edit(config, source, replacement, prompt, duration, emit):
        calls["source_editor"] += 1
        assert config["keys"]["higgsfield"] and not config["keys"]["openai"] and not config["keys"]["fal"]
        assert duration == selected_seconds
        meta = media.probe(source)
        assert meta["has_audio"] is False
        assert meta["duration"] == pytest.approx(selected_seconds, abs=1 / media.FPS)
        # Keep the fake boundary compatible with the real adapter's input contract.
        source_editor._validate_source(source, duration)
        shutil.copyfile(clips[1], replacement)
        await emit("source_video_edit", "higgsfield", config["models"]["video_edit_model"],
                   "Offline fake source editing response", "completed")

    def tracked_stitch(*args, **kwargs):
        calls["stitch"] += 1
        assert kwargs["maximum"] == 30
        if fail_first_assembly and calls["stitch"] == 1:
            raise media.MediaError("Simulated local assembly interruption")
        return actual_stitch(*args, **kwargs)

    monkeypatch.setattr(source_editor, "edit_source_video", fake_source_edit)
    monkeypatch.setattr(media, "stitch", tracked_stitch)
    endpoint = f"/api/videos/{VIDEO_ID}/edits/{edit['id']}/generate"
    generated = wait_job(client, client.post(endpoint))
    if fail_first_assembly:
        assert generated["status"] == "failed", generated
        assert "assembly interruption" in generated["error"]
        assert calls == {"source_editor": 1, "stitch": 1}
        # Changing settings must not relabel a clip already generated by the old model.
        main.CONFIG["models"]["video_edit_model"] = "different-model-after-stitch-failure"
        generated = wait_job(client, client.post(endpoint))
        assert any(event["stage"] == "reuse-replacement" for event in generated["events"])
    assert generated["status"] == "completed", generated
    assert calls == {"source_editor": 1, "stitch": 2 if fail_first_assembly else 1}
    result = generated["result"]["edit"]
    assert result["strategy"] == "source_video" and result["status"] == "completed"
    assert result["run_events"] == generated["events"]
    assert main.load_edit(VIDEO_ID, edit["id"])["run_events"] == generated["events"]
    assert result["provenance"] == "Higgsfield API · bytedance/seedance-2.5/video-edit"
    assert "not been automatically verified" in result["review"]
    assert "error" not in result
    folder = main.edit_folder(VIDEO_ID, edit["id"])
    output, original = folder / "output.mp4", main.DATA / VIDEO_ID / "video.mp4"
    assert media.probe(output)["duration"] == pytest.approx(34, abs=1 / media.FPS)
    assert media.probe(output)["has_audio"] is True
    assert decoded_audio(output) == decoded_audio(original)
    assert media.probe(folder / "source-clip.mp4")["has_audio"] is False
    assert result["source_clip_url"].endswith("/source-clip.mp4")
    for at in (1, 33):
        red, _, blue = pixel_at(output, at)
        assert red > 220 and blue < 30
    red, _, blue = pixel_at(output, 2 + selected_seconds / 2)
    assert blue > 220 and red < 30
    for name in ("start", "end"):
        before = client.get(result["anchors"][name]["url"])
        after = client.get(result["edited_anchors"][name]["url"])
        assert before.status_code == after.status_code == 200
        with Image.open(io.BytesIO(before.content)) as image:
            red, _, blue = image.convert("RGB").getpixel((160, 160))
            assert red > 220 and blue < 30
        with Image.open(io.BytesIO(after.content)) as image:
            red, _, blue = image.convert("RGB").getpixel((160, 160))
            assert blue > 220 and red < 30
        assert result["edited_anchors"][name]["timestamp"] == pytest.approx(
            result["anchors"][name]["timestamp"], abs=0.001)
    expected_calls = calls.copy()
    reopened = wait_job(client, client.post(endpoint))
    assert reopened["status"] == "completed" and calls == expected_calls
    assert reopened["result"]["edit"]["run_events"] == generated["events"]
    assert main.load_edit(VIDEO_ID, edit["id"])["run_events"] == generated["events"]
    assert not any(event["provider"] in ("openai", "fal") for event in generated["events"])


def test_source_generation_explains_missing_higgsfield_key(client, monkeypatch):
    install_fake_planner(monkeypatch, [])
    planned = plan(client, 2, 6)
    assert planned["status"] == "completed", planned
    main.CONFIG["keys"]["higgsfield"] = ""
    edit = planned["result"]["edit"]
    response = client.post(f"/api/videos/{VIDEO_ID}/edits/{edit['id']}/generate")
    assert response.status_code == 400
    assert "Higgsfield" in response.json()["detail"]
    assert main.load_edit(VIDEO_ID, edit["id"])["status"] == "planned"


def test_environment_stills_never_generate_video_and_cache_per_video(client, monkeypatch):
    main.CONFIG['keys']['openai'] = 'fake-image-key'
    calls = []
    async def fake_image(config, source, target, instruction, emit, label):
        calls.append(label)
        await asyncio.sleep(.03)
        Image.new('RGB', (320, 320), 'green').save(target)
        await emit(f'edit_{label}_anchor', 'openai', config['models']['image_model'], 'Fake image complete.', 'completed')
    monkeypatch.setattr(main.providers, 'edit_image', fake_image)
    source = (main.DATA / VIDEO_ID / 'video.mp4').read_bytes()
    assert client.get(f'/api/videos/{VIDEO_ID}/environment-previews').json()['status'] == 'not_started'
    started = client.post(f'/api/videos/{VIDEO_ID}/environment-previews')
    duplicate = client.post(f'/api/videos/{VIDEO_ID}/environment-previews')
    assert duplicate.json()['job_id'] == started.json()['job_id']
    result = wait_job(client, started)
    previews = result['result']['previews']
    assert previews['status'] == 'completed'
    assert set(calls) == {'golf-course', 'festival', 'courtside', 'airport'}
    assert len(calls) == 4
    assert all(item['status'] == 'completed' for item in previews['previews'])
    for item in previews['previews']:
        assert client.get(item['url']).headers['content-type'] == 'image/png'
    assert client.get(f'/api/videos/{VIDEO_ID}/edits').json()['edits'] == []
    assert (main.DATA / VIDEO_ID / 'video.mp4').read_bytes() == source
    assert all(job['type'] == 'environment_previews' for job in main.JOBS.values())
    main.JOBS.clear()
    reused = client.post(f'/api/videos/{VIDEO_ID}/environment-previews').json()
    assert reused['reused'] and len(calls) == 4
    assert client.get(f'/api/videos/{VIDEO_ID}/environment-previews').json()['status'] == 'completed'
    # Media metadata is private even though PNG previews can be served.
    assert client.get(f'/media/{VIDEO_ID}/environment-previews.json').status_code == 404


def test_preview_failure_does_not_auto_retry_paid_images(client, monkeypatch):
    main.CONFIG['keys']['openai'] = 'fake-image-key'
    calls = []
    async def fake_image(config, source, target, instruction, emit, label):
        calls.append(label)
        if label == 'airport' and calls.count(label) == 1:
            raise main.providers.ProviderError('Image service temporarily unavailable')
        Image.new('RGB', (320, 320), 'blue').save(target)
    monkeypatch.setattr(main.providers, 'edit_image', fake_image)
    result = wait_job(client, client.post(f'/api/videos/{VIDEO_ID}/environment-previews'))
    assert result['result']['previews']['status'] == 'failed'
    assert len(calls) == 4
    assert client.post(f'/api/videos/{VIDEO_ID}/environment-previews').json()['reused']
    assert len(calls) == 4
    result = wait_job(client, client.post(f'/api/videos/{VIDEO_ID}/environment-previews?retry_failed=true'))
    assert result['result']['previews']['status'] == 'completed'
    assert len(calls) == 5 and calls.count('airport') == 2


def test_preview_requires_image_key_and_excludes_static_videos(client):
    assert client.post(f'/api/videos/{VIDEO_ID}/environment-previews').status_code == 400
    presets = client.get('/api/environments').json()['environments']
    assert len(presets) == 4 and all('example_url' not in preset for preset in presets)
