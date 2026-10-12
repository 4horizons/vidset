"""Offline contracts for Higgsfield uploads, idempotency, polling and recovery."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app import source_editor as s
from app.providers import ProviderError

REAL_CLIENT = httpx.AsyncClient
REQUEST_ID = "ce21285d-26c0-435c-a275-e68a9c153ac3"
STATUS_URL = f"https://api.higgsfield.ai/requests/{REQUEST_ID}/status"
UPLOAD_URL = "https://higgsfield-uploads.s3.us-east-1.amazonaws.com/source.mp4?signature=temporary"
PUBLIC_URL = "https://cdn.higgsfield.ai/source.mp4"
VIDEO_URL = "https://cdn.higgsfield.ai/result.mp4"
VIDEO_BYTES = b"fake-mp4-payload-used-only-in-mocked-http-tests"
CONFIG = {"keys": {"higgsfield": "test-id:test-secret"}, "models": {"video_edit_model": s.MODEL}}
META = {"duration": 5.1667, "width": 406, "height": 720, "fps": 24.0}


class SourceEditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source.mp4"
        self.source.write_bytes(VIDEO_BYTES)
        self.output = self.folder / "replacement.mp4"
        self.config = copy.deepcopy(CONFIG)
        self.events, self.calls = [], []
        self.probe = patch.object(s.media, "probe", return_value=META.copy())
        self.probe.start()
        self.addCleanup(self.probe.stop)

    async def emit(self, stage, provider, model, message, status="running", artifacts=None):
        self.events.append({"stage": stage, "provider": provider, "model": model, "message": message, "status": status})

    def transport(self, handler):
        transport = httpx.MockTransport(handler)
        return patch.object(s.httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs))

    async def edit(self, prompt="Replace only the background with a beach.", duration=5.1667):
        await s.edit_source_video(self.config, self.source, self.output, prompt, duration, self.emit)

    def handler(self, request):
        self.calls.append(request)
        if request.url.path == "/files/generate-upload-url":
            self.assertEqual(request.headers["authorization"], "Key test-id:test-secret")
            self.assertEqual(json.loads(request.content), {"content_type": "video/mp4"})
            return httpx.Response(200, json={"upload_url": UPLOAD_URL, "public_url": PUBLIC_URL,
                "upload_headers": {"Content-Type": "video/mp4", "x-amz-tagging": "retention=temporary"}})
        if request.method == "PUT":
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(request.headers["x-amz-tagging"], "retention=temporary")
            self.assertEqual(request.headers["content-type"], "video/mp4")
            self.assertEqual(int(request.headers["content-length"]), len(VIDEO_BYTES))
            self.assertEqual(request.content, VIDEO_BYTES)
            return httpx.Response(200)
        if request.method == "POST":
            self.assertEqual(str(request.url), f"{s.API}/{s.MODEL}")
            self.assertEqual(request.headers["authorization"], "Key test-id:test-secret")
            self.assertTrue(request.headers["idempotency-key"])
            body = json.loads(request.content)
            self.assertEqual(body["video_url"], PUBLIC_URL)
            self.assertEqual(body["resolution"], "720p")
            self.assertEqual(body["output_format"], "mp4")
            self.assertFalse(body["generate_audio"])
            self.assertNotIn("duration", body)
            self.assertNotIn("aspect_ratio", body)
            return httpx.Response(200, json={"status": "queued", "request_id": REQUEST_ID, "status_url": STATUS_URL})
        if request.url.path.endswith("/status"):
            self.assertEqual(request.headers["authorization"], "Key test-id:test-secret")
            return httpx.Response(200, json={"status": "completed", "request_id": REQUEST_ID, "video": {"url": VIDEO_URL}})
        self.assertEqual(str(request.url), VIDEO_URL)
        self.assertNotIn("authorization", request.headers)
        return httpx.Response(200, content=VIDEO_BYTES)

    def ticket(self):
        return json.loads(self.output.with_suffix(".higgsfield-job.json").read_text())

    async def test_upload_submit_poll_and_download_use_real_contract(self):
        with self.transport(self.handler):
            await self.edit()
        self.assertEqual(self.output.read_bytes(), VIDEO_BYTES)
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(self.ticket()["state"], "completed")
        self.assertEqual(self.ticket()["request_id"], REQUEST_ID)
        self.assertNotIn("test-secret", json.dumps(self.ticket()))
        self.assertNotIn("signature=temporary", json.dumps(self.ticket()))
        self.assertEqual(self.events[-1]["status"], "completed")
        self.assertEqual(self.output.with_suffix(".higgsfield-job.json").stat().st_mode & 0o777, 0o600)

    async def test_polling_uses_lowercase_queued_and_in_progress(self):
        phases = iter(["queued", "in_progress", "completed"])
        def handler(request):
            if request.url.path.endswith("/status"):
                return httpx.Response(200, json={"status": next(phases), "video": {"url": VIDEO_URL}})
            return self.handler(request)
        with self.transport(handler), patch.object(s, "POLL_SECONDS", 0):
            await self.edit()
        self.assertTrue(any("is queued" in item["message"] for item in self.events))
        self.assertTrue(any("original motion" in item["message"] for item in self.events))

    async def test_ambiguous_submission_replays_same_key_body_without_upload(self):
        submissions = []
        def handler(request):
            if request.method == "POST" and request.url.path.endswith("/video-edit"):
                submissions.append((request.headers["idempotency-key"], json.loads(request.content)))
                if len(submissions) == 1:
                    raise httpx.ReadTimeout("sensitive provider internals test-secret", request=request)
            return self.handler(request)
        with self.transport(handler):
            with self.assertRaises(ProviderError):
                await self.edit()
            self.assertEqual(self.ticket()["state"], "submitting")
            await self.edit()
        self.assertEqual(submissions[0], submissions[1])
        self.assertEqual(sum(r.method == "PUT" for r in self.calls), 1)
        self.assertNotIn("test-secret", json.dumps(self.events))

    async def test_failed_status_request_resumes_ticket_without_resubmission(self):
        attempts = []
        def handler(request):
            if request.url.path.endswith("/status"):
                attempts.append(1)
                if len(attempts) == 1:
                    return httpx.Response(503, text="private provider error")
            return self.handler(request)
        with self.transport(handler):
            with self.assertRaises(ProviderError):
                await self.edit()
            self.assertEqual(self.ticket()["state"], "active")
            await self.edit()
        self.assertEqual(sum(r.url.path.endswith("/video-edit") for r in self.calls), 1)
        self.assertTrue(any("no new generation" in item["message"] for item in self.events))

    async def test_download_failure_reuses_completed_result(self):
        attempts = []
        def handler(request):
            if str(request.url) == VIDEO_URL:
                attempts.append(1)
                if len(attempts) == 1:
                    return httpx.Response(503)
            return self.handler(request)
        with self.transport(handler):
            with self.assertRaises(ProviderError):
                await self.edit()
            self.assertEqual(self.ticket()["state"], "completed")
            await self.edit()
        self.assertEqual(sum(r.method == "POST" for r in self.calls), 2)  # upload initiation + one generation
        self.assertEqual(sum(r.url.path.endswith("/status") for r in self.calls), 1)
        self.assertEqual(self.output.read_bytes(), VIDEO_BYTES)

    async def test_changed_inputs_and_broken_tickets_do_not_submit_again(self):
        with self.transport(self.handler):
            await self.edit()
        with self.transport(lambda request: self.fail("Must not make any network request")):
            with self.assertRaisesRegex(ProviderError, "different inputs"):
                await self.edit(prompt="A different requested edit")
            self.output.with_suffix(".higgsfield-job.json").write_text("{broken")
            with self.assertRaisesRegex(ProviderError, "unreadable"):
                await self.edit()

    async def test_terminal_failures_are_sanitized_and_never_resubmitted(self):
        def handler(request):
            if request.url.path.endswith("/status"):
                return httpx.Response(200, json={"status": "failed", "error": "private video details test-secret"})
            return self.handler(request)
        with self.transport(handler):
            with self.assertRaises(ProviderError):
                await self.edit()
        self.assertNotIn("test-secret", json.dumps(self.events))
        with self.transport(lambda request: self.fail("Terminal request must not resubmit")):
            with self.assertRaisesRegex(ProviderError, "fresh generation"):
                await self.edit()

    async def test_api_errors_never_expose_body_or_credentials(self):
        with self.transport(lambda request: httpx.Response(401, text="private test-secret transcript")):
            with self.assertRaisesRegex(ProviderError, "HTTP 401") as error:
                await self.edit()
        self.assertNotIn("test-secret", str(error.exception))
        self.assertNotIn("transcript", json.dumps(self.events))

    async def test_upload_rejects_ssrf_and_credential_bearing_headers(self):
        for url, headers in [
            ("https://127.0.0.1/upload", {"Content-Type": "video/mp4"}),
            ("https://higgsfield.ai.evil.example/upload", {"Content-Type": "video/mp4"}),
            (UPLOAD_URL, {"Content-Type": "video/mp4", "Authorization": "private-value"}),
            (UPLOAD_URL, {"Content-Type": "video/mp4", "Host": "127.0.0.1"}),
        ]:
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(200, json={"upload_url": url, "public_url": PUBLIC_URL, "upload_headers": headers})
            with self.subTest(url=url, headers=list(headers)), self.transport(handler):
                with self.assertRaises(ProviderError):
                    await self.edit()
            self.assertEqual(len(calls), 1)

    async def test_status_url_cannot_leak_provider_credentials(self):
        def handler(request):
            if request.url.path.endswith("/video-edit"):
                self.calls.append(request)
                return httpx.Response(200, json={"request_id": REQUEST_ID, "status_url": "https://evil.example/status"})
            return self.handler(request)
        with self.transport(handler):
            with self.assertRaisesRegex(ProviderError, "unsupported URL"):
                await self.edit()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.ticket()["state"], "submitting")

    async def test_download_redirect_cannot_reach_private_host(self):
        def handler(request):
            if str(request.url) == VIDEO_URL:
                return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
            return self.handler(request)
        with self.transport(handler):
            with self.assertRaisesRegex(ProviderError, "unsupported URL"):
                await self.edit()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix(".mp4.part").exists())

    async def test_download_size_is_bounded_even_without_content_length(self):
        def handler(request):
            response = self.handler(request)
            if str(request.url) == VIDEO_URL:
                del response.headers["content-length"]
            return response
        with self.transport(handler), patch.object(s, "MAX_DOWNLOAD_BYTES", 12):
            with self.assertRaisesRegex(ProviderError, "download limit"):
                await self.edit()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix(".mp4.part").exists())

    async def test_source_constraints_fail_before_any_network_call(self):
        cases = [("duration", 3.9), ("width", 299), ("width", 6001), ("width", 3000), ("fps", 12)]
        with self.transport(lambda request: self.fail("Invalid source must not be uploaded")):
            for field, value in cases:
                with self.subTest(field=field, value=value), patch.object(s.media, "probe", return_value={**META, field: value}):
                    with self.assertRaises(ProviderError):
                        await self.edit()
            for duration in (True, float("nan"), 3, 31):
                with self.assertRaises(ProviderError):
                    await self.edit(duration=duration)

    async def test_frame_subtraction_roundoff_accepts_limits_and_recovers_same_ticket(self):
        for actual, exact in [(3.9999999999999996, 4), (30.000000000000004, 30)]:
            with self.subTest(duration=actual):
                self.output.with_suffix(".higgsfield-job.json").unlink(missing_ok=True)
                self.output.unlink(missing_ok=True)
                self.calls.clear()
                with self.transport(self.handler), patch.object(s.media, "probe", return_value={**META, "duration": exact}):
                    await self.edit(duration=actual)
                    original_ticket = self.ticket()
                    await self.edit(duration=exact)
                self.assertEqual(self.ticket()["fingerprint"], original_ticket["fingerprint"])
                self.assertEqual(sum(r.url.path.endswith("/video-edit") for r in self.calls), 1)
                self.assertEqual(self.output.read_bytes(), VIDEO_BYTES)

    async def test_missing_key_and_unverified_models_fail_before_network(self):
        with self.transport(lambda request: self.fail("Must not call network")):
            self.config["keys"]["higgsfield"] = ""
            with self.assertRaisesRegex(ProviderError, "KEY_ID:KEY_SECRET"):
                await self.edit()
            self.config = copy.deepcopy(CONFIG)
            self.config["models"]["video_edit_model"] = "some/other/model"
            with self.assertRaisesRegex(ProviderError, "supports"):
                await self.edit()

    def test_only_safe_https_storage_urls_are_accepted(self):
        for url in [UPLOAD_URL, PUBLIC_URL, "https://d12345.cloudfront.net/file.mp4", "https://account.r2.cloudflarestorage.com/bucket/file.mp4"]:
            self.assertEqual(s._https_url(url), url)
        for url in ["http://cdn.higgsfield.ai/a", "https://user:secret@cdn.higgsfield.ai/a", "https://cdn.higgsfield.ai:8443/a", "https://[::1]/a", "https://cdn.higgsfield.ai/a#fragment", "https://evil.example/a"]:
            with self.subTest(url=url), self.assertRaises(ProviderError):
                s._https_url(url)


if __name__ == "__main__":
    unittest.main()
