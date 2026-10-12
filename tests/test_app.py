import copy
import time

import pytest
from fastapi.testclient import TestClient

from app import main, media


def wait_job(client, response):
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    for _ in range(1500):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.02)
    pytest.fail("Local job did not finish")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path)
    config = copy.deepcopy(main.CONFIG)
    config["keys"] = {k: "" for k in config["keys"]}
    monkeypatch.setattr(main, "CONFIG", config)
    main.JOBS.clear()
    main.VIDEO_LOCKS.clear()
    with TestClient(main.app) as client:
        yield client


@pytest.mark.skipif(not media.available(), reason="Requires ffmpeg")
def test_full_demo_upload_qa_edit_pipeline(client):
    created = wait_job(client, client.post("/api/demo"))
    assert created["status"] == "completed", created
    video = created["result"]["video"]
    assert video["demo"] and video["duration"] == pytest.approx(12, abs=0.1)
    assert len(video["frames"]) == 12
    vid = video["id"]
    answer = wait_job(client, client.post(f"/api/videos/{vid}/chat", json={"message": "When does the person pick up the tool?"}))
    assert answer["status"] == "completed", answer
    assert "00:05" in answer["result"]["answer"]
    assert answer["result"]["model"] == "Scripted demo"
    assert len(answer["result"]["evidence"]) == 2
    history = client.get(f"/api/videos/{vid}/conversation").json()["messages"]
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[1]["text"] == answer["result"]["answer"]
    assert history[1]["evidence"] == answer["result"]["evidence"]
    for evidence in answer["result"]["evidence"]:
        assert client.get(evidence["url"]).headers["content-type"] == "image/jpeg"
    planned = wait_job(client, client.post(f"/api/videos/{vid}/edits/plan", json={"instruction": "Add a swing and change the hydrant to yellow", "start": 6, "end": 10}))
    assert planned["status"] == "completed", planned
    edit = planned["result"]["edit"]
    generated = wait_job(client, client.post(f"/api/videos/{vid}/edits/{edit['id']}/generate"))
    assert generated["status"] == "completed", generated
    edited = generated["result"]["edit"]
    assert edited["status"] == "completed" and edited["edited_anchors"]
    assert {event["provider"] for event in generated["events"]} == {"local"}
    response = client.get(edited["output_url"], headers={"Range": "bytes=0-99"})
    assert response.status_code == 206 and len(response.content) == 100
    output = main.DATA / vid / "edits" / edit["id"] / "output.mp4"
    assert media.probe(output)["duration"] == pytest.approx(video["duration"], abs=1/media.FPS)
    # A repeated click reopens the saved output rather than generating another paid job.
    reopened = wait_job(client, client.post(f"/api/videos/{vid}/edits/{edit['id']}/generate"))
    assert reopened["result"]["edit"]["output_url"] == edited["output_url"]
    assert len(reopened["events"]) == 1
    # The same clip uploaded by a user is not allowed to receive fabricated demo analysis.
    saved_video = (main.DATA / vid / "video.json").read_bytes()
    saved_edit = (main.DATA / vid / "edits" / edit["id"] / "edit.json").read_bytes()
    saved_output = output.read_bytes()
    with (main.DATA / vid / "video.mp4").open("rb") as source:
        uploaded = wait_job(client, client.post("/api/videos", files={"file": ("my-video.mp4", source, "video/mp4")}))
    own = uploaded["result"]["video"]
    assert not own["demo"]
    # Adding and reopening another video must preserve the first workspace.
    assert own["id"] != vid
    assert {item["id"] for item in client.get("/api/videos").json()["videos"]} == {vid, own["id"]}
    assert client.get(f"/api/videos/{own['id']}/edits").json()["edits"] == []
    assert client.get(f"/api/videos/{own['id']}/conversation").json()["messages"] == []
    main.JOBS.clear()  # Saved history and edits also survive losing in-memory jobs.
    assert client.get(f"/api/videos/{vid}/conversation").json()["messages"] == history
    assert client.get(f"/api/videos/{vid}").json()["analysis"]
    assert client.get(f"/api/videos/{vid}/edits").json()["edits"][0]["output_url"] == edited["output_url"]
    assert (main.DATA / vid / "video.json").read_bytes() == saved_video
    assert (main.DATA / vid / "edits" / edit["id"] / "edit.json").read_bytes() == saved_edit
    assert output.read_bytes() == saved_output
    no_keys = wait_job(client, client.post(f"/api/videos/{own['id']}/analyze"))
    assert no_keys["status"] == "failed" and "API key" in no_keys["error"]


def test_config_is_secret_safe_and_cross_origin_changes_rejected(client):
    value = "test-secret-not-a-live-key"
    response = client.post("/api/config", json={"keys": {"gemini": value}})
    assert response.status_code == 200
    assert value not in response.text and response.json()["providers"]["gemini"]["configured"]
    assert value not in client.get("/api/config").text
    denied = client.post("/api/config", json={"keys": {"gemini": "evil"}}, headers={"Origin": "https://example.com"})
    assert denied.status_code == 403
    assert main.CONFIG["keys"]["gemini"] == value
    invalid = client.post("/api/config", json={"models": {"vision_provider": "invalid"}})
    assert invalid.status_code == 400
    assert client.get("/api/config", headers={"host": "attacker.example"}).status_code == 400


def test_upload_and_route_validation(client):
    assert client.post("/api/videos", files={"file": ("script.html", b"hi")}).status_code == 400
    assert client.post("/api/videos", files={"file": ("empty.mp4", b"")}).status_code == 400
    assert client.get("/api/videos/../../etc/passwd").status_code == 404
    assert client.get("/api/videos/missing/conversation").status_code == 404
    failed = wait_job(client, client.post("/api/videos", files={"file": ("invalid.mp4", b"not a video")}))
    assert failed["status"] == "failed"
    assert "FFmpeg" in failed["error"]
