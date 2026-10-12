"""Higgsfield Seedance 2.5 source-video editing, with recoverable paid requests.

Official API contracts verified October 2026:
https://open.higgsfield.ai/models/bytedance/seedance-2.5/video-edit/api-reference
https://docs.higgsfield.ai/docs/concepts/file-uploads
https://docs.higgsfield.ai/docs/concepts/requests
https://docs.higgsfield.ai/docs/concepts/idempotency

This adapter edits the uploaded motion itself. Original audio is restored by
the caller's local assembly, so generation requests disable generated audio.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import media
from .providers import ProviderError, _http_error, _json_request

API = "https://api.higgsfield.ai"
MODEL = "bytedance/seedance-2.5/video-edit"
MAX_SOURCE_BYTES = 200 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
TIMEOUT_SECONDS = 20 * 60
POLL_SECONDS = 2.0
MAX_POLL_SECONDS = 10.0
DURATION_EPSILON = 1e-5


def _https_url(value, *, api=False):
    """Allow only the provider API or its normal public CDN/storage services.

    The upload docs return storage URLs dynamically rather than promising one
    hostname. No arbitrary URLs, IP literals, custom ports or credentials are
    accepted. A new storage service requires a deliberate allowlist update.
    """
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) <= 32 for c in value) or "\\" in value:
        raise ProviderError("Higgsfield returned an invalid media URL.")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
            raise ValueError
        if api:
            allowed = host == "api.higgsfield.ai"
        else:
            allowed = (
                host == "higgsfield.ai" or host.endswith(".higgsfield.ai")
                or bool(re.fullmatch(r"[a-z0-9-]+\.cloudfront\.net", host))
                or bool(re.fullmatch(r"[a-z0-9.-]+\.r2\.cloudflarestorage\.com", host))
                or bool(re.fullmatch(r"(?:[a-z0-9.-]+\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host))
            )
        if not allowed:
            raise ValueError
    except (ValueError, TypeError):
        raise ProviderError("Higgsfield returned an unsupported URL host. Check the provider dashboard; no request was sent to that host.") from None
    return value


def _status_url(value, request_id):
    value = _https_url(value, api=True)
    parsed = urlsplit(value)
    if parsed.path != f"/requests/{request_id}/status" or parsed.query:
        raise ProviderError("Higgsfield returned an unexpected status URL. Check the provider dashboard before retrying.")
    return value


def _save_ticket(path, ticket):
    temporary = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            os.chmod(temporary, 0o600)
            json.dump(ticket, target, sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    except OSError:
        raise ProviderError("Could not save the Higgsfield request ticket. Check disk space and provider history before retrying.") from None


def _body(prompt, public_url):
    # Do not send duration/aspect_ratio: video-edit inherits the source timeline.
    return {"prompt": prompt, "video_url": public_url, "resolution": "720p",
            "output_format": "mp4", "generate_audio": False}


def _load_ticket(path, fingerprint, prompt):
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 64 * 1024:
            raise ValueError
        ticket = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(ticket, dict) or ticket.get("version") != 1:
            raise ValueError
        if ticket.get("fingerprint") != fingerprint:
            raise ProviderError("This source edit has a saved request for different inputs. Create a new edit plan for changed inputs.")
        if ticket.get("model") != MODEL or ticket.get("state") not in {"submitting", "active", "completed", "terminal_failed"}:
            raise ValueError
        uuid.UUID(ticket["idempotency_key"])
        body = ticket["body"]
        if not isinstance(body, dict) or body != _body(prompt, _https_url(body.get("video_url"))):
            raise ValueError
        if ticket["state"] != "submitting":
            uuid.UUID(ticket["request_id"])
            _status_url(ticket["status_url"], ticket["request_id"])
        if ticket["state"] == "completed":
            _https_url(ticket["video_url"])
        return ticket
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        raise ProviderError("The saved Higgsfield request ticket is unreadable. Check the provider dashboard; no new generation was submitted.") from None


def _source_fingerprint(source, model, prompt, duration):
    try:
        if source.suffix.lower() != ".mp4" or not 16 <= source.stat().st_size <= MAX_SOURCE_BYTES:
            raise ProviderError("Prepare a source MP4 under 200 MB before editing.")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        payload = {"source_sha256": digest.hexdigest(), "model": model, "prompt": prompt,
                   "duration": duration, "resolution": "720p", "generate_audio": False}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    except OSError:
        raise ProviderError("The selected source clip could not be read. Prepare the edit again.") from None


def _validate_source(source, duration):
    try:
        meta = media.probe(source)
    except (media.MediaError, OSError, ValueError, KeyError):
        raise ProviderError("The selected source MP4 could not be inspected. Prepare the edit again.") from None
    # Conservative compatibility bounds for the Seedance 2.5 video input family.
    # The local feature intentionally limits selected source edits to 4–30 s.
    if not 4 - DURATION_EPSILON <= meta["duration"] <= 30.05 or abs(meta["duration"] - duration) > 0.15:
        raise ProviderError("Source editing needs a 4–30 second MP4 matching the selected interval.")
    if not all(300 <= meta[key] <= 6000 for key in ("width", "height")) or not 0.4 <= meta["width"] / meta["height"] <= 2.5:
        raise ProviderError("Prepare the source with sides between 300 and 6000 pixels and an aspect ratio between 0.4 and 2.5.")
    if not 23.9 <= meta["fps"] <= 60.1:
        raise ProviderError("Prepare the source MP4 at 24–60 frames per second.")


async def _upload_source(client, source, auth):
    result = await _json_request(client, "Higgsfield", "POST", f"{API}/files/generate-upload-url",
                                 headers=auth, json={"content_type": "video/mp4"})
    upload_url = _https_url(result.get("upload_url"))
    public_url = _https_url(result.get("public_url"))
    headers = result.get("upload_headers")
    if not isinstance(headers, dict) or not headers or len(headers) > 32:
        raise ProviderError("Higgsfield returned invalid upload headers; no source was uploaded.")
    for name, value in headers.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", name)
                or name.lower() in {"authorization", "proxy-authorization", "cookie", "host", "content-length", "transfer-encoding"}
                or not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise ProviderError("Higgsfield returned unsafe upload headers; no source was uploaded.")
    if {name.lower(): value for name, value in headers.items()}.get("content-type") != "video/mp4":
        raise ProviderError("Higgsfield returned an incompatible upload content type.")

    async def chunks():
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                yield chunk

    try:
        # API credentials are never client defaults and never reach the storage host.
        response = await client.put(upload_url, headers={**headers, "Content-Length": str(source.stat().st_size)}, content=chunks())
    except (httpx.HTTPError, OSError):
        raise ProviderError("The source upload did not finish. Retry the edit; no video generation was submitted.") from None
    if response.status_code >= 300:
        raise _http_error("Higgsfield source upload", response.status_code)
    return public_url


async def _download(client, url, output):
    part = output.with_suffix(output.suffix + ".part")
    try:
        for _ in range(4):
            url = _https_url(url)
            async with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code >= 300:
                    raise _http_error("Higgsfield video download", response.status_code)
                length = response.headers.get("content-length")
                if length is not None and (not length.isdigit() or int(length) > MAX_DOWNLOAD_BYTES):
                    raise ProviderError("The edited video exceeds the 256 MB download limit.")
                size = 0
                with part.open("wb") as target:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ProviderError("The edited video exceeds the 256 MB download limit.")
                        target.write(chunk)
                if size < 16:
                    raise ProviderError("Higgsfield returned an empty video file.")
                part.replace(output)
                return
        raise ProviderError("The edited video redirected too many times. Check the Higgsfield dashboard.")
    except (httpx.HTTPError, OSError):
        raise ProviderError("Could not download the edited video. Retry to retrieve the saved generation without another paid submission.") from None
    finally:
        part.unlink(missing_ok=True)


async def edit_source_video(config, source_path: Path, output_path: Path, prompt: str, duration: float, emit) -> None:
    """Upload once, submit with stable idempotency, resume, and download an edit.

    The caller serializes jobs for an edit and validates/stitches the resulting
    video. No provider secrets, signed upload URLs, or response errors are logged.
    """
    model = config.get("models", {}).get("video_edit_model") or MODEL
    key = config.get("keys", {}).get("higgsfield", "")
    if (not isinstance(key, str) or len(key) > 2048
            or not re.fullmatch(r"[!-9;-~]+:[!-9;-~]+", key.strip())):
        raise ProviderError("Add your Higgsfield API KEY_ID:KEY_SECRET in Models & keys to edit source video.")
    key = key.strip()
    if model != MODEL:
        raise ProviderError(f"This source editor supports {MODEL}. Choose that model in Settings.")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration)
            or not 4 - DURATION_EPSILON <= duration <= 30 + DURATION_EPSILON):
        raise ProviderError("Choose a source edit interval between 4 and 30 seconds.")
    # Subtracting frame-aligned endpoints can yield 3.9999999999999996 or
    # 30.000000000000004. Normalize before fingerprinting as well as validation,
    # so an equivalent range recovers the same saved generation ticket.
    duration = min(30, max(4, round(float(duration), 6)))
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 12000:
        raise ProviderError("Describe the source edit in 1–12000 characters.")
    source_path, output_path = Path(source_path), Path(output_path)
    fingerprint = await asyncio.to_thread(_source_fingerprint, source_path, model, prompt, duration)
    await asyncio.to_thread(_validate_source, source_path, duration)
    ticket_path = output_path.with_suffix(".higgsfield-job.json")
    ticket = _load_ticket(ticket_path, fingerprint, prompt)
    if ticket and ticket["state"] == "terminal_failed":
        raise ProviderError("This Higgsfield generation ended without a video. Create a new edit plan for a fresh generation; this retry will not submit another.")
    auth = {"Authorization": f"Key {key}"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False, trust_env=False) as client:
            if ticket is None:
                await emit("source_upload", "higgsfield", model, "Uploading the selected source interval to Higgsfield for motion-preserving editing.")
                public_url = await _upload_source(client, source_path, auth)
                ticket = {"version": 1, "model": model, "fingerprint": fingerprint, "state": "submitting",
                          "idempotency_key": str(uuid.uuid4()), "body": _body(prompt, public_url)}
                _save_ticket(ticket_path, ticket)
                await emit("source_upload", "higgsfield", model, "Source uploaded; its exact request and retry key are saved locally.", status="completed")
            else:
                await emit("reuse-source-upload", "local", "Saved upload", "Reusing the saved source upload; no video was uploaded again.", status="completed")
            if ticket["state"] == "submitting":
                await emit("source_video_edit", "higgsfield", model, "Submitting the source-video edit with a saved idempotency key; retries reuse this same generation intent.")
                result = await _json_request(client, "Higgsfield", "POST", f"{API}/{model}",
                                             headers={**auth, "Idempotency-Key": ticket["idempotency_key"]}, json=ticket["body"])
                request_id = result.get("request_id")
                try:
                    uuid.UUID(request_id)
                except (ValueError, TypeError, AttributeError):
                    raise ProviderError("Higgsfield returned no valid request ID. Retry with the saved idempotency key to recover this request.") from None
                status_url = _status_url(result.get("status_url"), request_id)
                ticket.update(state="active", request_id=request_id, status_url=status_url)
                _save_ticket(ticket_path, ticket)
            else:
                await emit("source_video_edit", "higgsfield", model, f"Resuming saved request {ticket['request_id'][:8]}; no new generation submitted.")
            deadline, delay, prior = time.monotonic() + TIMEOUT_SECONDS, POLL_SECONDS, None
            while ticket["state"] != "completed":
                if time.monotonic() >= deadline:
                    raise ProviderError("Higgsfield is still processing after 20 minutes. Retry this saved edit to resume polling without another submission.")
                result = await _json_request(client, "Higgsfield", "GET", ticket["status_url"], headers=auth)
                phase = result.get("status")
                if phase in {"failed", "nsfw", "canceled"} or result.get("error"):
                    ticket["state"] = "terminal_failed"
                    _save_ticket(ticket_path, ticket)
                    raise ProviderError("Higgsfield could not complete this edit (failed, canceled, or content rejected). Check its dashboard; provider details are not logged.")
                if phase not in {"queued", "in_progress", "completed"}:
                    raise ProviderError("Higgsfield returned an unknown request status. Retry this saved edit to check it again.")
                if phase != prior:
                    messages = {"queued": "Seedance 2.5 source edit is queued.",
                                "in_progress": "Seedance 2.5 is editing the source video using its original motion.",
                                "completed": "Seedance 2.5 finished; downloading the edited clip."}
                    await emit("source_video_edit", "higgsfield", model, messages[phase], status="info")
                    prior = phase
                if phase == "completed":
                    video = result.get("video")
                    if not isinstance(video, dict):
                        raise ProviderError("Higgsfield completed without a readable video result. Retry to retrieve the saved request.")
                    ticket.update(state="completed", video_url=_https_url(video.get("url")))
                    _save_ticket(ticket_path, ticket)
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, MAX_POLL_SECONDS)
            await _download(client, ticket["video_url"], output_path)
    except ProviderError as exc:
        await emit("source_video_edit", "higgsfield", model, str(exc), status="failed")
        raise
    await emit("source_video_edit", "higgsfield", model, "Edited source video downloaded. Local assembly will restore the original audio and fit the selected interval.", status="completed")
