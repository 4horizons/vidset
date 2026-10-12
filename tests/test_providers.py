"""Offline provider contract tests. No API keys or paid calls are used."""
import base64
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from app import providers as p


BASE_CONFIG = {
    "keys": {"gemini": "test-gemini", "openai": "test-openai", "anthropic": "test-anthropic", "fal": "test-fal"},
    "models": {"vision_provider": "gemini", "vision_model": "gemini-2.5-flash", "chat_provider": "openai", "chat_model": "gpt-4.1-mini", "image_model": "gpt-image-1.5", "video_model": "fal-ai/kling-video/o1/image-to-video"},
}
REAL_ASYNC_CLIENT = httpx.AsyncClient


def gemini_response(data):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(data)}]}}]})


def openai_response(data):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(data)}}]})


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.picture = self.directory / "frame.png"
        Image.new("RGB", (640, 360), "#abcdef").save(self.picture)
        self.clip = self.directory / "clip.mp4"
        self.clip.write_bytes(b"small-fake-proxy-used-only-by-mock-transport")
        self.config = copy.deepcopy(BASE_CONFIG)
        self.events = []
        self.frames = [{"path": self.picture, "timestamp": float(time), "url": f"/media/test/f{time}.jpg"} for time in (0, 2, 4, 6)]

    async def emit(self, stage, provider, model, message, status="running", artifacts=None):
        self.events.append({"stage": stage, "provider": provider, "model": model, "message": message, "status": status})

    def transport(self, handler):
        mock = httpx.MockTransport(handler)
        return patch.object(p.httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_ASYNC_CLIENT(transport=mock, **kwargs))

    async def test_missing_key_is_actionable_and_does_not_call_network(self):
        self.config["keys"].clear()
        with self.transport(lambda request: self.fail("Unexpected network request")):
            with self.assertRaisesRegex(p.ProviderError, "gemini API key in Settings"):
                await p.analyze_video(self.config, self.clip, self.frames, 6, self.emit)

    async def test_gemini_video_and_original_research_taxonomy(self):
        def handler(request):
            self.assertEqual(request.url.host, "generativelanguage.googleapis.com")
            self.assertEqual(request.headers["x-goog-api-key"], "test-gemini")
            body = json.loads(request.content)
            parts = body["contents"][0]["parts"]
            self.assertEqual(parts[0]["inline_data"]["mime_type"], "video/mp4")
            self.assertEqual(base64.b64decode(parts[0]["inline_data"]["data"]), self.clip.read_bytes())
            self.assertIn("hook_analysis", parts[-1]["text"])
            return gemini_response({"summary": "A person moves a tool.", "events": [{"start": 2, "end": 30, "label": "Pick up", "description": "Hand moves toward the tool."}], "research": {"hook": "Close shot", "key_insights": ["Static camera"]}})
        with self.transport(handler):
            result = await p.analyze_video(self.config, self.clip, self.frames, 6, self.emit)
        self.assertEqual(result["events"][0]["end"], 6)
        self.assertEqual(result["research"]["hook"], "Close shot")
        self.assertEqual(result["provider"], "gemini")
        self.assertEqual([e["status"] for e in self.events], ["running", "completed"])

    async def test_openai_analysis_includes_timestamped_images_and_audio_limit(self):
        self.config["models"].update(vision_provider="openai", vision_model="gpt-4.1-mini")
        def handler(request):
            body = json.loads(request.content)
            content = body["messages"][-1]["content"]
            self.assertEqual(len([part for part in content if part["type"] == "image_url"]), 4)
            self.assertIn("2.000 seconds", content[2]["text"])
            return openai_response({"summary": "A static scene.", "events": []})
        with self.transport(handler):
            result = await p.analyze_video(self.config, self.clip, self.frames, 6, self.emit)
        self.assertIn("audio/speech", result["limitations"])
        self.assertEqual(result["provider"], "openai")

    async def test_anthropic_request_uses_image_blocks(self):
        self.config["models"].update(vision_provider="anthropic", vision_model="claude-sonnet-4-6")
        def handler(request):
            self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "claude-sonnet-4-6")
            block = body["messages"][0]["content"][1]
            self.assertEqual(block["source"]["media_type"], "image/jpeg")
            self.assertEqual(block["source"]["type"], "base64")
            return httpx.Response(200, json={"content": [{"type": "text", "text": '{"summary":"A person standing."}'}]})
        with self.transport(handler):
            result = await p.analyze_video(self.config, self.clip, self.frames, 6, self.emit)
        self.assertEqual(result["provider"], "anthropic")

    async def test_two_models_keep_evidence_tied_to_actual_image(self):
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            if request.url.host == "generativelanguage.googleapis.com":
                return gemini_response({"answer": "The tool is held by about 4s.", "evidence": [{"timestamp": 4, "caption": "The person holds the tool."}, {"timestamp": 3.5, "caption": "An unsupplied frame."}, {"timestamp": 4, "caption": "Duplicate"}], "suggested_range": {"start": 2, "end": 4}, "limitations": "Exact onset was not sampled."})
            body = json.loads(request.content)
            self.assertFalse(any(item["type"] == "image_url" for item in body["messages"][-1]["content"]))
            self.assertNotIn("An unsupplied frame", request.content.decode())
            return openai_response({"answer": "By 4s, they are holding it; the pickup is between 2–4s.", "limitations": "Estimated interval."})
        with self.transport(handler):
            result = await p.answer_question(self.config, "When is the tool picked up?", {}, self.frames, 6, [], self.emit)
        self.assertEqual(hosts, ["generativelanguage.googleapis.com", "api.openai.com"])
        self.assertEqual(result["evidence"], [{"timestamp": 4.0, "caption": "The person holds the tool."}])
        self.assertEqual(result["provider"], "openai")
        self.assertEqual({e["stage"] for e in self.events}, {"visual_verification", "answer_synthesis"})

    async def test_single_configured_model_does_not_fake_second_call(self):
        self.config["keys"].pop("openai")
        calls = []
        def handler(request):
            calls.append(request)
            return gemini_response({"answer": "The beginning is unchanged.", "evidence": []})
        with self.transport(handler):
            result = await p.answer_question(self.config, "What changed?", {}, self.frames, 6, [], self.emit)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["provider"], "gemini")
        self.assertIn("no separate", self.events[-1]["message"])
        self.assertEqual(self.events[-1]["status"], "info")

    async def test_provider_error_body_and_key_are_never_exposed(self):
        def handler(request):
            return httpx.Response(401, json={"error": "leaked secret test-gemini and private transcript"})
        with self.transport(handler):
            with self.assertRaises(p.ProviderError) as caught:
                await p.analyze_video(self.config, self.clip, self.frames, 6, self.emit)
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("test-gemini", json.dumps(self.events))
        self.assertEqual(self.events[-1]["status"], "failed")

    async def test_edit_plan_honors_fixed_user_times(self):
        with self.transport(lambda request: openai_response({"start": 0, "end": 5, "rationale": "Selected region", "prompt": "Make the hydrant yellow."})):
            result = await p.plan_edit(self.config, "Yellow hydrant", {}, 6, 2, 4, self.emit)
        self.assertEqual((result["start"], result["end"]), (2, 4))

    async def test_edit_plan_requires_grounded_interval_or_manual_range(self):
        with self.transport(lambda request: openai_response({"needs_range": True})):
            with self.assertRaisesRegex(p.ProviderError, "Set start and end"):
                await p.plan_edit(self.config, "Change something invisible", {}, 6, None, None, self.emit)
        with self.transport(lambda request: self.fail("Must validate range before spending tokens")):
            with self.assertRaises(p.ProviderError):
                await p.plan_edit(self.config, "Yellow hydrant", {}, 20, 1, 15, self.emit)

    async def test_image_edit_sends_multipart_and_writes_verified_png(self):
        buffer = io.BytesIO()
        Image.new("RGB", (1024, 1024), "yellow").save(buffer, "PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode()
        def handler(request):
            self.assertEqual(request.url.path, "/v1/images/edits")
            self.assertIn("multipart/form-data", request.headers["content-type"])
            self.assertIn(b'name="image[]"', request.content)
            self.assertIn(b'name="input_fidelity"', request.content)
            return httpx.Response(200, json={"data": [{"b64_json": encoded}]})
        output = self.directory / "edited.png"
        with self.transport(handler):
            await p.edit_image(self.config, self.picture, output, "Change hydrant to yellow.", self.emit, "start")
        with Image.open(output) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1024, 1024))  # Backend, not adapter, restores source size.
        self.assertEqual(self.events[-1]["status"], "completed")

    async def test_fal_queue_uses_both_anchors_and_cdn_get_has_no_key(self):
        calls = []
        status_calls = []
        def handler(request):
            calls.append(request)
            if request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual(body["duration"], "3")
                self.assertTrue(body["start_image_url"].startswith("data:image/jpeg;base64,"))
                self.assertTrue(body["end_image_url"].startswith("data:image/jpeg;base64,"))
                self.assertEqual(request.headers["authorization"], "Key test-fal")
                return httpx.Response(200, json={"request_id": "test-request-id", "status_url": "https://queue.fal.run/fal-ai/kling-video/requests/test/status", "response_url": "https://queue.fal.run/fal-ai/kling-video/requests/test"})
            if request.url.path.endswith("/status"):
                status_calls.append(1)
                return httpx.Response(200, json={"status": "IN_PROGRESS" if len(status_calls) == 1 else "COMPLETED"})
            if request.url.host == "queue.fal.run":
                return httpx.Response(200, json={"video": {"url": "https://v3b.fal.media/files/test.mp4"}})
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, content=b"fake-mp4-contents-for-mock-only")
        output = self.directory / "generated.mp4"
        with self.transport(handler), patch.object(p, "FAL_POLL_SECONDS", 0):
            await p.generate_video(self.config, self.picture, self.picture, output, "Continue motion.", 1.5, self.emit)
        self.assertTrue(output.exists())
        self.assertEqual(len(calls), 5)
        self.assertTrue(any("Kling is generating" in event["message"] for event in self.events))
        self.assertEqual(self.events[-1]["status"], "completed")

    async def test_fal_queue_rejects_credential_exfiltration_url(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"request_id": "test-request-id", "status_url": "https://attacker.example/status", "response_url": "https://queue.fal.run/result"})
        with self.transport(handler):
            with self.assertRaisesRegex(p.ProviderError, "unexpected queue URL"):
                await p.generate_video(self.config, self.picture, self.picture, self.directory / "out.mp4", "Edit", 4, self.emit)
        self.assertEqual(len(calls), 1)

    async def test_fal_completed_with_error_does_not_report_success(self):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={"request_id": "test-request-id", "status_url": "https://queue.fal.run/status", "response_url": "https://queue.fal.run/result"})
            return httpx.Response(200, json={"status": "COMPLETED", "error": "private provider details"})
        output = self.directory / "out.mp4"
        with self.transport(handler):
            with self.assertRaisesRegex(p.ProviderError, "generation failed"):
                await p.generate_video(self.config, self.picture, self.picture, output, "Edit", 4, self.emit)
        self.assertFalse(output.exists())
        self.assertNotIn("private provider details", json.dumps(self.events))
        with self.transport(lambda request: self.fail("A terminal failed ticket must not resubmit")):
            with self.assertRaisesRegex(p.ProviderError, "fresh paid generation"):
                await p.generate_video(self.config, self.picture, self.picture, output, "Edit", 4, self.emit)

    async def test_fal_retry_reuses_ticket_and_redownloads_completed_result(self):
        calls = []
        downloads = []
        output = self.directory / "replacement.mp4"
        def handler(request):
            calls.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"request_id": "same-remote-job", "status_url": "https://queue.fal.run/status", "response_url": "https://queue.fal.run/result"})
            if request.url.path == "/status":
                return httpx.Response(200, json={"status": "COMPLETED"})
            if request.url.path == "/result":
                return httpx.Response(200, json={"video": {"url": "https://v3b.fal.media/replacement.mp4"}})
            downloads.append(1)
            return httpx.Response(503) if len(downloads) == 1 else httpx.Response(200, content=b"fake-mp4-contents-for-mock-only")
        with self.transport(handler):
            with self.assertRaises(p.ProviderError):
                await p.generate_video(self.config, self.picture, self.picture, output, "Edit", 4, self.emit)
            self.assertFalse(output.exists())
            cached = json.loads(output.with_suffix(".job.json").read_text())
            self.assertEqual(cached["state"], "completed")
            self.assertEqual(cached["request_id"], "same-remote-job")
            self.assertNotIn("test-fal", json.dumps(cached))
            await p.generate_video(self.config, self.picture, self.picture, output, "Edit", 4, self.emit)
        self.assertTrue(output.exists())
        self.assertEqual(sum(r.method == "POST" for r in calls), 1)
        self.assertEqual(sum(r.url.path == "/status" for r in calls), 1)
        self.assertEqual(len(downloads), 2)
        self.assertTrue(any("Resuming saved fal request" in event["message"] for event in self.events))
        with self.transport(lambda request: self.fail("Changed inputs require an explicit new plan")):
            with self.assertRaisesRegex(p.ProviderError, "different inputs"):
                await p.generate_video(self.config, self.picture, self.picture, output, "Different edit", 4, self.emit)

    async def test_fal_ambiguous_submission_is_not_blindly_resubmitted(self):
        output = self.directory / "replacement.mp4"
        def handler(request):
            raise httpx.ReadTimeout("secret raw error", request=request)
        with self.transport(handler):
            with self.assertRaises(p.ProviderError):
                await p.generate_video(self.config, self.picture, self.picture, output, "Edit", 4, self.emit)
        with self.transport(lambda request: self.fail("Ambiguous POST must not be retried automatically")):
            with self.assertRaisesRegex(p.ProviderError, "previous submission"):
                await p.generate_video(self.config, self.picture, self.picture, output, "Edit", 4, self.emit)

    async def test_fal_malformed_saved_ticket_never_submits_new_job(self):
        output = self.directory / "replacement.mp4"
        output.with_suffix(".job.json").write_text("{not-valid-json")
        with self.transport(lambda request: self.fail("Malformed ticket must not cause paid resubmission")):
            with self.assertRaisesRegex(p.ProviderError, "saved generation ticket is unreadable"):
                await p.generate_video(self.config, self.picture, self.picture, output, "Edit", 4, self.emit)

    async def test_large_gemini_proxy_is_rejected_before_network(self):
        with self.clip.open("wb") as clip:
            clip.truncate(18 * 1024 * 1024)
        with self.transport(lambda request: self.fail("Oversize proxy should be rejected locally")):
            with self.assertRaisesRegex(p.ProviderError, "too large"):
                await p.analyze_video(self.config, self.clip, self.frames, 6, self.emit)

    async def test_malformed_optional_analysis_fields_do_not_crash(self):
        with self.transport(lambda request: gemini_response({"summary": "A visible scene.", "events": {"wrong": "type"}, "research": {"key_insights": 123}})):
            result = await p.analyze_video(self.config, self.clip, self.frames, 6, self.emit)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["research"]["key_insights"], [])

    async def test_malformed_provider_envelope_gives_safe_error(self):
        with self.transport(lambda request: httpx.Response(200, json={"candidates": ["wrong"]})):
            with self.assertRaises(p.ProviderError):
                await p.analyze_video(self.config, self.clip, self.frames, 6, self.emit)

    def test_json_and_range_validation(self):
        self.assertEqual(p._parse_json('```json\n{"answer":"yes"}\n```'), {"answer": "yes"})
        self.assertEqual(p._parse_json('Here is JSON: {"answer":"yes"}'), {"answer": "yes"})
        with self.assertRaises(p.ProviderError):
            p._parse_json("I cannot do this")
        self.assertIsNone(p._range({"start": float("nan"), "end": 3}, 6))
        for unsafe in ("http://v3b.fal.media/a.mp4", "https://fal.media.evil.example/a", "https://127.0.0.1/a", "https://user:secret@v3b.fal.media/a"):
            with self.assertRaises(p.ProviderError):
                p._download_url(unsafe)


if __name__ == "__main__":
    unittest.main()
