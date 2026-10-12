"""Use one fal key for routed LLMs and Seedance source-video editing.

Verified contracts:
https://fal.ai/models/openrouter/router/api
https://fal.ai/models/openrouter/router/vision/api
https://fal.ai/models/openrouter/router/video/api
https://fal.ai/models/bytedance/seedance-2.5/reference-to-video/api

Provider helpers are imported inside functions so providers can route here
without a module-import cycle. No account credentials are read at import time.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

DEFAULT_VISION_MODEL = "google/gemini-2.5-flash"
DEFAULT_CHAT_MODEL = "openai/gpt-4.1-mini"
SOURCE_MODEL = "bytedance/seedance-2.5/reference-to-video"
MAX_ANALYSIS_BYTES = 14 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
POLL_SECONDS = 3
TIMEOUT_SECONDS = 20 * 60


async def call_json(config, model, prompt, frames=None, video_path=None):
    """Route a text, timestamped-image, or whole-video JSON request through fal."""
    from . import providers as p

    key = p._key(config, "fal")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./:-]{1,160}", model):
        raise p.ProviderError("Use an OpenRouter model ID such as google/gemini-2.5-flash for fal vision or reasoning.")
    selected = p._frames(frames or [])
    if video_path is not None and selected:
        raise p.ProviderError("Supply either the analysis video or evidence images to this fal call.")
    body = {"prompt": prompt, "model": model, "system_prompt": p.GROUNDING,
            "max_tokens": 8192, "temperature": 0, "reasoning": False, "enable_web_search": False}
    endpoint = "openrouter/router"
    if video_path is not None:
        path = Path(video_path)
        try:
            if not 16 <= path.stat().st_size <= MAX_ANALYSIS_BYTES:
                raise p.ProviderError("The fal analysis video must be under 14 MB. Use a shorter analysis proxy.")
            body["video_urls"] = [p._data_uri(path.read_bytes(), "video/mp4")]
        except OSError:
            raise p.ProviderError("The analysis video could not be read. Upload the video again.") from None
        endpoint += "/video"
    elif selected:
        body["image_urls"] = [p._data_uri(p._image_bytes(frame["path"])) for frame in selected]
        labels = "; ".join(f"Image {index + 1}: {float(frame['timestamp']):.3f} seconds" for index, frame in enumerate(selected))
        body["prompt"] = f"Evidence images are supplied in this exact order: {labels}.\n{prompt}"
        endpoint += "/vision"
    if len(json.dumps(body).encode()) > 32 * 1024 * 1024:
        raise p.ProviderError("The fal media request is too large. Use fewer or smaller evidence frames.")
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15), follow_redirects=False, trust_env=False) as client:
        result = await p._json_request(client, "fal model router", "POST", f"https://fal.run/{endpoint}",
                                       headers={"Authorization": f"Key {key}"}, json=body)
    if result.get("error") or result.get("partial"):
        raise p.ProviderError("The fal model router did not finish a complete answer. Retry or choose another model.")
    return p._parse_json(result.get("output"))


def _download_url(value):
    from .providers import ProviderError

    try:
        if not isinstance(value, str) or len(value) > 8192 or "\\" in value or any(ord(c) <= 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        fal_host = any(host == suffix or host.endswith("." + suffix) for suffix in ("fal.media", "fal.ai", "fal.run"))
        # Official model schemas also use this fal-owned Google Storage bucket.
        google_host = host == "storage.googleapis.com" and parsed.path.startswith("/falserverless/")
        if (parsed.scheme != "https" or not (fal_host or google_host) or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError
    except (ValueError, TypeError):
        raise ProviderError("fal returned an unsupported video download URL. Retrieve the saved result from its dashboard.") from None
    return value


async def _download(client, url, output):
    from . import providers as p

    part = output.with_suffix(output.suffix + ".part")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(4):
            url = _download_url(url)
            async with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code >= 300:
                    raise p._http_error("fal video download", response.status_code)
                length = response.headers.get("content-length")
                if length is not None and (not length.isdigit() or int(length) > MAX_DOWNLOAD_BYTES):
                    raise p.ProviderError("The fal result exceeds the 256 MB download limit.")
                size = 0
                with part.open("wb") as target:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise p.ProviderError("The fal result exceeds the 256 MB download limit.")
                        target.write(chunk)
                if size < 16:
                    raise p.ProviderError("fal returned an empty video.")
                part.replace(output)
                return
        raise p.ProviderError("The fal video redirected too many times. Check its dashboard.")
    except (httpx.HTTPError, OSError):
        raise p.ProviderError("Could not download the fal video. Retry this edit to retrieve the saved generation.") from None
    finally:
        part.unlink(missing_ok=True)


def _fingerprint(source, prompt, duration, model):
    from .providers import ProviderError

    try:
        if source.suffix.lower() != ".mp4" or not 16 <= source.stat().st_size <= MAX_SOURCE_BYTES:
            raise ProviderError("The fal source MP4 must be under 64 MB. Select a shorter interval or compress the clip.")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        data = {"source_sha256": digest.hexdigest(), "prompt": prompt, "duration": duration,
                "model": model, "task": "editing", "resolution": "720p", "generate_audio": False, "codec": "H264"}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    except OSError:
        raise ProviderError("The selected source clip could not be read. Prepare the edit again.") from None


async def edit_source_video(config, source_path, output_path, prompt, duration, emit):
    """Send original motion to Seedance on fal, retaining a durable queue ticket."""
    from . import providers as p
    from .source_editor import _validate_source

    key = p._key(config, "fal")
    model = config.get("models", {}).get("fal_video_edit_model") or SOURCE_MODEL
    if model != SOURCE_MODEL:
        raise p.ProviderError(f"Choose {SOURCE_MODEL} for source-video editing through fal.")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration)
            or not 4 - 1e-5 <= duration <= 30 + 1e-5):
        raise p.ProviderError("Choose a source edit interval between 4 and 30 seconds.")
    duration = min(30, max(4, round(float(duration), 6)))
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 12000:
        raise p.ProviderError("Describe the source edit in 1–12000 characters.")
    source_path, output_path = Path(source_path), Path(output_path)
    fingerprint = await asyncio.to_thread(_fingerprint, source_path, prompt, duration, model)
    await asyncio.to_thread(_validate_source, source_path, duration)
    ticket_path = output_path.with_suffix(".fal-source-job.json")
    ticket = p._load_generation_ticket(ticket_path, fingerprint)
    if ticket and ticket["state"] == "terminal_failed":
        raise p.ProviderError("This fal source edit failed or was canceled. Create a new edit plan for a fresh generation; this retry will not resubmit it.")
    headers = {"Authorization": f"Key {key}"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False, trust_env=False) as client:
            if ticket is None:
                await emit("source_upload", "fal", model, "Preparing the source clip for submission to fal with its original motion.")
                try:
                    body = {"prompt": "Edit @Video1 as the sole source video. " + prompt, "task": "editing",
                            "video_urls": [p._data_uri(source_path.read_bytes(), "video/mp4")],
                            "resolution": "720p", "generate_audio": False, "codec": "H264"}
                except OSError:
                    raise p.ProviderError("The selected source clip could not be read. Prepare the edit again.") from None
                # Fal has no assumed idempotency contract here. Persist intent
                # first and never blindly resubmit after an ambiguous POST.
                p._save_generation_ticket(ticket_path, {"version": 1, "fingerprint": fingerprint, "model": model, "state": "submitting"})
                result = await p._json_request(client, "fal", "POST", f"https://queue.fal.run/{model}", headers=headers, json=body)
                request_id = result.get("request_id")
                if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
                    raise p.ProviderError("fal returned no valid request ID. Check its dashboard before creating another edit.")
                ticket = {"version": 1, "fingerprint": fingerprint, "model": model, "state": "active", "request_id": request_id,
                          "status_url": p._queue_url(result.get("status_url")), "response_url": p._queue_url(result.get("response_url"))}
                p._save_generation_ticket(ticket_path, ticket)
                await emit("source_upload", "fal", model, "Source clip submitted with the edit; its queue ticket is saved locally.", status="completed")
            else:
                await emit("reuse-source-upload", "local", "Saved fal request", "Reusing the source submitted with the saved request; no new upload.", status="completed")
            await emit("source_video_edit", "fal", model, f"Tracking Seedance source edit {ticket['request_id'][:12]}; retries use this saved request.")
            deadline, prior = time.monotonic() + TIMEOUT_SECONDS, None
            while time.monotonic() < deadline:
                result = {"status": "COMPLETED"} if ticket["state"] == "completed" else await p._json_request(client, "fal", "GET", ticket["status_url"], headers=headers)
                phase = result.get("status")
                if phase in ("FAILED", "CANCELLED") or result.get("error") or result.get("error_type"):
                    ticket["state"] = "terminal_failed"
                    p._save_generation_ticket(ticket_path, ticket)
                    raise p.ProviderError("fal source editing failed or was canceled. Check the dashboard; this request will not be submitted again automatically.")
                if phase != prior:
                    messages = {"IN_QUEUE": "Seedance source edit is queued at fal.", "IN_PROGRESS": "Seedance is editing the original video motion.",
                                "COMPLETED": "Seedance finished; retrieving the edited video."}
                    await emit("source_video_edit", "fal", model, messages.get(phase, "Checking the fal source-edit request."), status="info")
                    prior = phase
                if phase == "COMPLETED":
                    ticket["state"] = "completed"
                    p._save_generation_ticket(ticket_path, ticket)
                    result = await p._json_request(client, "fal", "GET", ticket["response_url"], headers=headers)
                    if result.get("error") or not isinstance(result.get("video"), dict):
                        raise p.ProviderError("fal returned no readable edited video. Retry to retrieve this saved request.")
                    await _download(client, result["video"].get("url"), output_path)
                    break
                if phase not in ("IN_QUEUE", "IN_PROGRESS"):
                    raise p.ProviderError("fal returned an unknown queue status. Retry this saved edit to check again.")
                await asyncio.sleep(POLL_SECONDS)
            else:
                raise p.ProviderError("fal is still processing after 20 minutes. Retry this saved edit to resume without another submission.")
    except p.ProviderError as exc:
        await emit("source_video_edit", "fal", model, str(exc), status="failed")
        raise
    await emit("source_video_edit", "fal", model, "Edited video downloaded. Local assembly will restore the original audio.", status="completed")
