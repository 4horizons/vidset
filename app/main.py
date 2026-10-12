"""Single-process, loopback-only API for Aurrra Video Lab."""
import asyncio
import copy
import json
import math
import os
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import demo, media, providers, source_editor, workflows, environments, routing

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
load_dotenv(ROOT / ".env")  # Deliberately never traverse to the production repository.
CONFIG = {"keys": {name: os.getenv(env, "") for name, env in {
    "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "fal": "FAL_KEY", "higgsfield": "HIGGSFIELD_API_KEY"}.items()},
    "models": {"vision_provider": os.getenv("VISION_PROVIDER", "gemini"),
               "vision_model": os.getenv("VISION_MODEL", "gemini-2.5-flash"),
               "chat_provider": os.getenv("CHAT_PROVIDER", "openai"),
               "chat_model": os.getenv("CHAT_MODEL", "gpt-4.1-mini"),
               "image_model": os.getenv("IMAGE_MODEL", "gpt-image-1.5"),
               "video_edit_provider": os.getenv("VIDEO_EDIT_PROVIDER", "higgsfield"),
               "fal_video_edit_model": os.getenv("FAL_VIDEO_EDIT_MODEL", "bytedance/seedance-2.5/reference-to-video"),
               "video_edit_model": os.getenv("VIDEO_EDIT_MODEL", "bytedance/seedance-2.5/video-edit"),
               "video_model": os.getenv("VIDEO_MODEL", "fal-ai/kling-video/o1/image-to-video")}}
JOBS = {}
TASKS = set()
VIDEO_LOCKS = {}
WORKERS = asyncio.Semaphore(2)


def configure_available_models(config, explicit=None):
    """Fill unavailable default routes from keys the owner actually supplied."""
    explicit = explicit or {}
    keys, models = config["keys"], config["models"]
    for role in ("vision", "chat"):
        field = f"{role}_provider"
        if field in explicit or keys.get(models.get(field)):
            continue
        provider = next((name for name in ("openai", "gemini", "anthropic", "fal") if keys.get(name)), None)
        if provider:
            models[field] = provider
            models[f"{role}_model"] = ("openai/gpt-4.1-mini" if role == "chat" and provider == "fal" else providers.DEFAULTS[provider])
    if "video_edit_provider" not in explicit and not keys.get(models.get("video_edit_provider", "higgsfield")):
        if keys.get("higgsfield"):
            models["video_edit_provider"] = "higgsfield"
        elif keys.get("fal"):
            models["video_edit_provider"] = "fal"


configure_available_models(CONFIG)


@asynccontextmanager
async def lifespan(app):
    yield
    for task in list(TASKS):
        task.cancel()
    if TASKS:
        await asyncio.gather(*TASKS, return_exceptions=True)


app = FastAPI(title="Aurrra Video Lab", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])


@app.middleware("http")
async def local_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
        if origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "Open the app on its local address to make changes."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def public_config():
    return {"providers": {name: {"configured": bool(key)} for name, key in CONFIG["keys"].items()},
            "models": CONFIG["models"].copy(), "limits": {"max_mb": 128, "max_seconds": media.MAX_SECONDS},
            "ffmpeg": media.available()}


class ConfigRequest(BaseModel):
    keys: dict[str, str] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict)


@app.get("/api/config")
async def get_config():
    return public_config()


@app.get("/api/routing")
async def get_routing():
    return routing.apply(CONFIG, list(routing.TITLES))["_routing"]


@app.post("/api/config")
async def set_config(body: ConfigRequest):
    for name, value in body.keys.items():
        if name not in CONFIG["keys"] or len(value) > 2048:
            raise HTTPException(400, "Invalid provider key setting.")
    for name, value in body.models.items():
        if name not in CONFIG["models"] or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,150}", value):
            raise HTTPException(400, "Invalid model setting.")
        if name in ("vision_provider", "chat_provider") and value not in providers.DEFAULTS:
            raise HTTPException(400, "Choose Gemini, OpenAI, Anthropic, or fal.ai for vision and chat.")
        if name == "video_edit_provider" and value not in ("higgsfield", "fal"):
            raise HTTPException(400, "Choose Higgsfield or fal.ai for source video editing.")
    for name, value in body.keys.items():
        if value.strip():
            CONFIG["keys"][name] = value.strip()
    CONFIG["models"].update(body.models)
    configure_available_models(CONFIG, body.models)
    return public_config()


def identifier():
    return uuid.uuid4().hex


def directory(video_id):
    if not re.fullmatch(r"[a-f0-9]{32}", video_id):
        raise HTTPException(404, "Video not found.")
    path = DATA / video_id
    if not path.is_dir():
        raise HTTPException(404, "Video not found.")
    return path


def load_video(video_id):
    path = directory(video_id) / "video.json"
    if not path.exists():
        raise HTTPException(404, "Video is still being prepared or is unavailable.")
    return json.loads(path.read_text())


def write_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))
    tmp.replace(path)


def save_video(video):
    write_json(directory(video["id"]) / "video.json", video)


def asset_url(video_id, path):
    return f"/media/{video_id}/{Path(path).relative_to(directory(video_id)).as_posix()}"


async def extract_frames(video, stamps):
    folder = directory(video["id"])
    result = []
    for stamp in sorted(set(stamps)):
        quantized = media.frame_time(float(stamp), video["duration"])
        path = folder / "frames" / f"frame-{round(quantized * media.FPS):06d}.jpg"
        actual = await asyncio.to_thread(media.frame, folder / "video.mp4", quantized, path, video["duration"])
        result.append({"timestamp": actual, "url": asset_url(video["id"], path), "path": path})
    return result


def public_frames(frames):
    return [{k: v for k, v in frame.items() if k != "path"} for frame in frames]


def clean_error(exc):
    if isinstance(exc, (providers.ProviderError, media.MediaError, ValueError)):
        message = str(exc)[:600]
    elif isinstance(exc, HTTPException):
        message = str(exc.detail)
    else:
        message = f"This step failed ({type(exc).__name__}). Retry it or check the selected model and video format."
    for secret in CONFIG["keys"].values():
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def start_job(kind, worker, video_id=None, workflow=None):
    # Keep observability bounded in a long-running hackathon session.
    if len(JOBS) >= 200:
        for key in list(JOBS):
            if JOBS[key]["status"] in ("completed", "failed"):
                del JOBS[key]
                if len(JOBS) < 160:
                    break
    job_id = identifier()
    job = {"id": job_id, "type": kind, "status": "queued", "events": [], "result": None, "error": None,
           "workflow": copy.deepcopy(workflow or {"title": kind.title(), "steps": []}),
           "routing": copy.deepcopy((workflow or {}).get("routing")),
           "created_at": datetime.now(timezone.utc).isoformat(), "started_at": None, "completed_at": None}
    JOBS[job_id] = job
    beginnings = {}

    async def emit(stage, provider, model, message, status="running", artifacts=None):
        event = {"id": len(job["events"]) + 1, "stage": stage, "provider": provider, "model": model,
                 "message": message, "status": status, "time": datetime.now(timezone.utc).isoformat()}
        key = (stage, provider, model)
        if status == "running":
            beginnings.setdefault(key, time.monotonic())
        elif status in ("completed", "failed", "skipped") and key in beginnings:
            event["elapsed_ms"] = round((time.monotonic() - beginnings.pop(key)) * 1000)
        if artifacts:
            event["artifacts"] = artifacts
        job["events"].append(event)

    async def run():
        try:
            async with WORKERS:
                lock = VIDEO_LOCKS.setdefault(video_id or job_id, asyncio.Lock())
                async with lock:
                    job["status"] = "running"
                    job["started_at"] = datetime.now(timezone.utc).isoformat()
                    if job.get("routing"):
                        choices = job["routing"]["decisions"]
                        selected = [f"{item['chosen']['label']}: {item['reason']}" for item in choices if item.get("chosen")]
                        await emit("model_selection", "local", "Routing policy", "; ".join(selected), "completed")
                    job["result"] = await worker(emit)
                    job["completed_at"] = datetime.now(timezone.utc).isoformat()
                    result_edit = job["result"].get("edit") if isinstance(job["result"], dict) else None
                    if result_edit and not job["result"].get("reused"):
                        result_edit["run_events"] = copy.deepcopy(job["events"])
                        result_edit["job_id"] = job_id
                        result_edit["workflow"] = copy.deepcopy(job["workflow"])
                        result_edit["run_started_at"] = job["started_at"]
                        result_edit["run_completed_at"] = job["completed_at"]
                        write_json(edit_folder(result_edit["video_id"], result_edit["id"]) / "edit.json", result_edit)
                    job["status"] = "completed"
        except asyncio.CancelledError:
            job["status"] = "failed"
            job["error"] = "Server stopped before the job finished. Please retry."
            job["completed_at"] = datetime.now(timezone.utc).isoformat()
            await emit("error", "local", "Job runner", job["error"], "failed")
            raise
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = clean_error(exc)
            job["completed_at"] = datetime.now(timezone.utc).isoformat()
            await emit("error", "local", "Job runner", job["error"], "failed")
    task = asyncio.create_task(run())
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "This job is unavailable. The server may have restarted.")
    return JOBS[job_id]


@app.get("/api/videos")
async def list_videos():
    videos = []
    for path in sorted(DATA.glob("*/video.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            videos.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return {"videos": videos}


@app.get("/api/environments")
async def get_environments():
    return {"environments": environments.options(ROOT / "app" / "static")}


def load_environment_previews(video_id):
    path = directory(video_id) / "environment-previews.json"
    if not path.exists():
        return {"status": "not_started", "previews": []}
    saved = json.loads(path.read_text())
    if saved["status"] in ("queued", "running") and JOBS.get(saved.get("job_id"), {}).get("status") not in ("queued", "running"):
        saved["status"] = "failed"
        saved["error"] = "Still previews were interrupted. Retry missing previews when ready."
        for preview in saved["previews"]:
            if preview["status"] != "completed":
                preview["status"] = "failed"
        write_json(path, saved)
    return saved


@app.get("/api/videos/{video_id}/environment-previews")
async def get_environment_previews(video_id: str):
    load_video(video_id)
    return load_environment_previews(video_id)


@app.post("/api/videos/{video_id}/environment-previews")
async def create_environment_previews(video_id: str, retry_failed: bool = False):
    """Image-only operation. Video generation is a separate, explicit endpoint."""
    video = load_video(video_id)
    if video["demo"]:
        raise HTTPException(400, "Upload a real clip to create AI setting previews.")
    saved = load_environment_previews(video_id)
    if saved["status"] in ("queued", "running"):
        return {"job_id": saved["job_id"]}
    if saved["status"] != "not_started" and not (retry_failed and saved["status"] == "failed"):
        return {"previews": saved, "reused": True}
    config = routing.for_job(CONFIG, "environment_previews", video)
    if not config["keys"].get("openai"):
        raise HTTPException(400, "Add an OpenAI key in Models to create still previews.")
    folder = directory(video_id) / "environment-previews"
    folder.mkdir(exist_ok=True)
    path = directory(video_id) / "environment-previews.json"
    previous = {item["id"]: item for item in saved["previews"] if item["status"] == "completed"}
    saved = {"status": "queued", "previews": [previous.get(item["id"], {"id": item["id"], "status": "queued"}) for item in environments.ENVIRONMENTS],
             "provider": "openai", "model": config["models"]["image_model"]}
    workflow = {"title": "Create four still previews", "steps": [workflows._step("preview_frame", "Extract a source still")] + [
        workflows._step(f"edit_{item['id']}_anchor", f"Preview: {item['name']}", "openai", config["models"]["image_model"], skip_on=[f"reuse-preview-{item['id']}"])
        for item in environments.ENVIRONMENTS]}

    workflow["routing"] = config.get("_routing")

    async def worker(emit):
        saved["status"] = "running"
        write_json(path, saved)
        await emit("preview_frame", "local", "FFmpeg", "Extracting one real source frame. This job only creates still images.")
        frame = (await extract_frames(video, [min(video["duration"] * .75, 29.0)]))[0]
        saved["source_frame"] = {k: frame[k] for k in ("url", "timestamp")}
        await emit("preview_frame", "local", "FFmpeg", f"Using the source frame at {frame['timestamp']:.2f}s.", "completed", [{**saved["source_frame"], "label": "Original preview frame"}])
        slots = asyncio.Semaphore(2)

        async def render(item, result):
            if result["status"] == "completed":
                await emit(f"reuse-preview-{item['id']}", "local", "Saved still", f"Reusing {item['name']} preview.", "completed")
                return
            async with slots:
                result["status"] = "running"
                write_json(path, saved)
                try:
                    output = folder / f"{item['id']}.png"
                    instruction = (f"Place the main foreground subject in {item['setting']}. " + environments.PRESERVE +
                                   " This is a single concept still, not a video. Keep the subject in exactly the same position and preserve all readable product artwork.")
                    await providers.edit_image(config, frame["path"], output, instruction, emit, item["id"])
                    result.update(status="completed", url=asset_url(video_id, output), timestamp=frame["timestamp"], model=config["models"]["image_model"])
                    result.pop("error", None)
                    await emit("preview_ready", "openai", config["models"]["image_model"], f"{item['name']} still saved. Full video waits for your click.", "completed", [{"url": result["url"], "label": item["name"] + " · still preview"}])
                except Exception as exc:
                    result.update(status="failed", error=clean_error(exc))
                write_json(path, saved)

        await asyncio.gather(*(render(item, result) for item, result in zip(environments.ENVIRONMENTS, saved["previews"])))
        saved["status"] = "completed" if all(item["status"] == "completed" for item in saved["previews"]) else "failed"
        write_json(path, saved)
        return {"previews": saved}

    created = start_job("environment_previews", worker, f"{video_id}:previews", workflow)
    saved["job_id"] = created["job_id"]
    write_json(path, saved)
    return created


@app.get("/api/videos/{video_id}")
async def get_video(video_id: str):
    return load_video(video_id)


def load_conversation(video_id):
    path = directory(video_id) / "conversation.json"
    if not path.exists():
        return []
    try:
        messages = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise HTTPException(500, "This video's saved conversation could not be read. Its source and edits are still available.") from None
    if not isinstance(messages, list):
        raise HTTPException(500, "This video's saved conversation has an invalid format.")
    return messages


@app.get("/api/videos/{video_id}/conversation")
async def get_conversation(video_id: str):
    return {"messages": load_conversation(video_id)}


async def prepare_video(video_id, name, emit, is_demo=False):
    folder = directory(video_id)
    await emit("prepare", "local", "FFmpeg", "Inspecting video and preparing a browser-compatible preview.")
    if is_demo:
        await asyncio.to_thread(demo.render, folder / "video.mp4")
        meta = await asyncio.to_thread(media.probe, folder / "video.mp4")
    else:
        meta = await asyncio.to_thread(media.normalize, folder / "upload.bin", folder / "video.mp4")
        (folder / "upload.bin").replace(folder / "original.bin")
    video = {"id": video_id, "name": name, **meta, "url": asset_url(video_id, folder / "video.mp4"),
             "demo": is_demo, "analysis": None}
    await emit("prepare", "local", "FFmpeg", f"Prepared {meta['duration']:.1f}s at {meta['width']} × {meta['height']}; original audio {'retained' if meta['has_audio'] else 'not present'}.", "completed")
    await emit("frames", "local", "FFmpeg", "Extracting a timestamped contact sheet.")
    frames = await extract_frames(video, media.sample_times(video["duration"]))
    video["frames"] = public_frames(frames)
    video["thumbnail"] = frames[0]["url"]
    save_video(video)
    await emit("frames", "local", "FFmpeg", f"Extracted {len(frames)} real frames from this clip.", "completed",
               [{"url": x["url"], "label": f"{x['timestamp']:.2f}s", "timestamp": x["timestamp"]} for x in frames])
    return {"video": video}


@app.post("/api/videos")
async def upload_video(file: UploadFile = File(...)):
    if not media.available():
        raise HTTPException(503, "Install FFmpeg and restart the app.")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"):
        raise HTTPException(400, "Upload an MP4, MOV, WebM, MKV, M4V, or AVI file.")
    video_id = identifier()
    folder = DATA / video_id
    folder.mkdir()
    size = 0
    try:
        with (folder / "upload.bin").open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > media.MAX_BYTES:
                    raise HTTPException(413, "Clip exceeds the 128 MB upload limit.")
                output.write(chunk)
        if not size:
            raise HTTPException(400, "The uploaded file is empty.")
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await file.close()
    name = Path(file.filename).name[:180]
    return start_job("upload", lambda emit: prepare_video(video_id, name, emit), video_id, workflows.build_workflow("upload", CONFIG))


@app.post("/api/demo")
async def create_demo():
    for path in DATA.glob("*/video.json"):
        video = json.loads(path.read_text())
        if video.get("demo"):
            async def existing(emit):
                await emit("demo", "local", "Scripted demo", "Opened the illustrated sample. No model calls.", "completed")
                return {"video": video}
            return start_job("demo", existing, video["id"], workflows.build_workflow("demo", CONFIG, video=video))
    video_id = identifier()
    (DATA / video_id).mkdir()
    return start_job("demo", lambda emit: prepare_video(video_id, "Neighborhood walk · illustrated demo", emit, True), video_id, workflows.build_workflow("demo", CONFIG))


async def analyze(video, config, emit):
    if video["demo"]:
        await emit("understand", "local", "Scripted demo", "Reading the illustrated fixture's known event timings. No AI inference.")
        video["analysis"] = demo.analysis()
        await emit("understand", "local", "Scripted demo", "Loaded three scripted scene events.", "completed")
    else:
        folder = directory(video["id"])
        await emit("proxy", "local", "FFmpeg", "Preparing a small video proxy for visual understanding.")
        proxy = await asyncio.to_thread(media.make_proxy, folder / "video.mp4", folder / "analysis.mp4")
        await emit("proxy", "local", "FFmpeg", f"Analysis proxy prepared ({proxy.stat().st_size / 1048576:.1f} MB).", "completed")
        frames = await extract_frames(video, [x["timestamp"] for x in video["frames"]])
        video["analysis"] = await providers.analyze_video(config, proxy, frames, video["duration"], emit)
    save_video(video)
    return video


@app.post("/api/videos/{video_id}/analyze")
async def analyze_endpoint(video_id: str):
    video = load_video(video_id)
    config = routing.for_job(CONFIG, "analyze", video)
    async def worker(emit):
        video = await analyze(load_video(video_id), config, emit)
        return {"video": video}
    return start_job("analyze", worker, video_id, workflows.build_workflow("analyze", config, video=video))


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


def relevant_times(video, question):
    """Coarse-to-fine retrieval; broad coverage remains alongside a relevant event window."""
    times = media.sample_times(video["duration"], count=10)
    words = set(re.findall(r"[a-z]{3,}", question.lower())) - {"the", "this", "does", "when", "what", "video", "person"}
    events = (video.get("analysis") or {}).get("events", [])
    ranked = []
    for event in events:
        terms = set(re.findall(r"[a-z]{3,}", f"{event.get('label','')} {event.get('description','')}".lower()))
        score = len(words & terms)
        if score:
            ranked.append((score, event))
    if ranked:
        event = max(ranked, key=lambda x: x[0])[1]
        start = max(0, float(event["start"]) - 0.75)
        end = min(video["duration"], float(event["end"]) + 0.75)
        times += media.sample_times(video["duration"], 8, start, end)
    for minute, sec in re.findall(r"\b(\d{1,2}):(\d{2})\b", question):
        at = min(video["duration"], int(minute) * 60 + int(sec))
        times += media.sample_times(video["duration"], 4, max(0, at-1), min(video["duration"], at+1))
    return sorted(set(times))[:26]


@app.post("/api/videos/{video_id}/chat")
async def chat_endpoint(video_id: str, body: ChatRequest):
    video = load_video(video_id)
    config = routing.for_job(CONFIG, "chat", video)
    async def worker(emit):
        video = load_video(video_id)
        if not video.get("analysis"):
            video = await analyze(video, config, emit)
        if video["demo"]:
            await emit("answer", "local", "Scripted demo", "Matching a supported sample question to known fixture events.")
            result = demo.answer(body.message)
            await emit("answer", "local", "Scripted demo", "Prepared a scripted answer; no models called.", "completed")
        else:
            await emit("retrieve", "local", "FFmpeg", "Extracting broad context and denser frames near matching scene events.")
            frames = await extract_frames(video, relevant_times(video, body.message))
            await emit("retrieve", "local", "FFmpeg", f"Prepared {len(frames)} timestamped frames for visual verification.", "completed",
                       [{"url": x["url"], "label": f"{x['timestamp']:.2f}s", "timestamp": x["timestamp"]} for x in frames])
            result = await providers.answer_question(config, body.message, video["analysis"], frames,
                                                     video["duration"], [x.model_dump() for x in body.history], emit)
        evidence = []
        for item in result.get("evidence", [])[:6]:
            stamp = float(item["timestamp"])
            if not math.isfinite(stamp) or not 0 <= stamp <= video["duration"]:
                continue
            actual = (await extract_frames(video, [stamp]))[0]
            evidence.append({"timestamp": actual["timestamp"], "url": actual["url"], "caption": item.get("caption", "Evidence frame")})
        result["evidence"] = evidence
        await emit("evidence", "local", "FFmpeg", f"Attached {len(evidence)} source frames to the answer.", "completed",
                   [{"url": x["url"], "label": x["caption"], "timestamp": x["timestamp"]} for x in evidence])
        # The per-video job lock serializes this append with other chat jobs.
        history = load_conversation(video_id)
        history.extend([
            {"id": identifier(), "role": "user", "mode": "ask", "text": body.message},
            {"id": identifier(), "role": "assistant", "text": result.get("answer", ""),
             **{key: result[key] for key in ("evidence", "suggested_range", "model", "provider", "limitations") if key in result}},
        ])
        write_json(directory(video_id) / "conversation.json", history[-200:])
        return result
    return start_job("chat", worker, video_id, workflows.build_workflow("chat", config, video=video))


class EditRequest(BaseModel):
    preset_id: Literal["golf-course", "festival", "courtside", "airport"] | None = None
    strategy: Literal["anchors", "source_video"] = "anchors"
    instruction: str = Field(min_length=3, max_length=4000)
    start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, gt=0, allow_inf_nan=False)


def edit_folder(video_id, edit_id):
    if not re.fullmatch(r"[a-f0-9]{32}", edit_id):
        raise HTTPException(404, "Edit not found.")
    return directory(video_id) / "edits" / edit_id


def load_edit(video_id, edit_id):
    path = edit_folder(video_id, edit_id) / "edit.json"
    if not path.exists():
        raise HTTPException(404, "Edit not found.")
    return json.loads(path.read_text())


@app.get("/api/videos/{video_id}/edits")
async def list_edits(video_id: str):
    folder = directory(video_id)
    edits = []
    for path in sorted(folder.glob("edits/*/edit.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        value = json.loads(path.read_text())
        if value["status"] == "generating" and JOBS.get(value.get("job_id"), {}).get("status") not in ("queued", "running"):
            value["status"] = "failed"
            value["error"] = "Generation interrupted by server restart. Check provider history before retrying."
            write_json(path, value)
        edits.append(value)
    return {"edits": edits}


@app.post("/api/videos/{video_id}/edits/plan")
async def plan_endpoint(video_id: str, body: EditRequest):
    video = load_video(video_id)
    strategy = "anchors" if video["demo"] else body.strategy
    minimum, maximum = (4, 30) if strategy == "source_video" else (1, 10)
    if (body.start is None) != (body.end is None):
        raise HTTPException(400, "Provide both start and end, or let the model locate the segment.")
    if body.start is not None:
        try:
            media.edit_range(body.start, body.end, video["duration"], minimum, maximum)
        except media.MediaError as exc:
            raise HTTPException(400, str(exc)) from exc
    config = routing.for_job(CONFIG, "plan", video)
    async def worker(emit):
        video = load_video(video_id)
        if not video.get("analysis"):
            video = await analyze(video, config, emit)
        if video["demo"]:
            if not any(x in body.instruction.lower() for x in ("hydrant", "yellow", "swing")):
                raise ValueError("The scripted demo supports adding a swing and making the hydrant yellow. Use your own upload and API keys for other edits.")
            await emit("plan", "local", "Scripted demo", "Planning the supported illustrated background edit.")
            plan = {"start": body.start if body.start is not None else 6.0, "end": body.end if body.end is not None else 10.0,
                    "rationale": "Scripted demonstration: change the illustrated background within the selected range.",
                    "prompt": body.instruction + ". Keep the walker, dog, houses, and camera motion consistent."}
            await emit("plan", "local", "Scripted demo", "Prepared a deterministic preview plan. No model called.", "completed")
        else:
            plan = await providers.plan_edit(config, body.instruction, video["analysis"], video["duration"], body.start, body.end, emit, strategy=strategy)
        start, end = media.edit_range(float(plan["start"]), float(plan["end"]), video["duration"], minimum, maximum)
        # A manually chosen range is authoritative over model output.
        if body.start is not None:
            start, end = media.edit_range(body.start, body.end, video["duration"], minimum, maximum)
        await emit("anchors", "local", "FFmpeg", f"Extracting source boundary frames for {start:.2f}–{end:.2f}s.")
        first, last = await extract_frames(video, [start, end - 1 / media.FPS])
        anchors = {"start": {k: first[k] for k in ("timestamp", "url")}, "end": {k: last[k] for k in ("timestamp", "url")}}
        edit_id = identifier()
        folder = edit_folder(video_id, edit_id)
        folder.mkdir(parents=True)
        shutil.copyfile(first["path"], folder / "start.jpg")
        shutil.copyfile(last["path"], folder / "end.jpg")
        edit = {"id": edit_id, "video_id": video_id, "instruction": body.instruction, "start": start, "end": end,
                "preset_id": body.preset_id,
                "status": "planned", "strategy": strategy, "rationale": str(plan.get("rationale", "")), "prompt": str(plan.get("prompt", body.instruction)),
                "anchors": anchors, "demo": video["demo"]}
        write_json(folder / "edit.json", edit)
        await emit("anchors", "local", "FFmpeg", "Boundary frames ready. Review the plan, then generate the replacement.", "completed",
                   [{**anchor, "label": label} for label, anchor in anchors.items()])
        return {"edit": edit}
    return start_job("plan", worker, video_id, workflows.build_workflow("plan", config, video=video))


async def generate_source_edit(video, edit, folder, config, emit):
    video_id = video["id"]
    source = directory(video_id) / "video.mp4"
    source_clip = folder / "source-clip.mp4"
    replacement = folder / "replacement.mp4"
    await emit("source_clip", "local", "FFmpeg", f"Extracting {edit['start']:.2f}–{edit['end']:.2f}s of source motion. The audio stays local.")
    await asyncio.to_thread(media.prepare_edit_source, source, source_clip, edit["start"], edit["end"])
    edit["source_clip_url"] = asset_url(video_id, source_clip)
    await emit("source_clip", "local", "FFmpeg", "Source clip ready; boundary screenshots are evidence, while the editor receives all video frames.", "completed",
               [{"url": edit["source_clip_url"], "label": "Source segment sent to the editor"}])
    valid = False
    if replacement.exists():
        try:
            await asyncio.to_thread(media.probe, replacement)
            valid = True
        except (media.MediaError, ValueError):
            replacement.unlink(missing_ok=True)
    if valid:
        await emit("reuse-replacement", "local", "Saved video", "Reusing the generated source edit without another paid call.", "completed")
    else:
        if config["models"].get("video_edit_provider", "higgsfield") == "fal":
            from . import fal_models
            await fal_models.edit_source_video(config, source_clip, replacement, edit["prompt"], round(edit["end"]-edit["start"], 6), emit)
            edit["provenance"] = "fal.ai API · " + config["models"]["fal_video_edit_model"]
        else:
            await source_editor.edit_source_video(config, source_clip, replacement, edit["prompt"], round(edit["end"]-edit["start"], 6), emit)
            edit["provenance"] = "Higgsfield API · " + config["models"].get("video_edit_model", "bytedance/seedance-2.5/video-edit")
        write_json(folder / "edit.json", edit)
    edit["replacement_url"] = asset_url(video_id, replacement)
    edit.setdefault("provenance", "Saved source edit · generation model unavailable")
    await emit("stitch", "local", "FFmpeg", "Matching duration and restoring the original full-clip audio.")
    meta = await asyncio.to_thread(media.stitch, source, replacement, folder / "output.mp4", edit["start"], edit["end"], maximum=30)
    edit["output_url"] = asset_url(video_id, folder / "output.mp4")
    edit["edited_anchors"] = {}
    for name, stamp in (("start", edit["start"]), ("end", edit["end"] - 1/media.FPS)):
        path = folder / f"edited-{name}.png"
        await asyncio.to_thread(media.frame, folder / "output.mp4", stamp, path, meta["duration"])
        edit["edited_anchors"][name] = {"url": asset_url(video_id, path), "timestamp": stamp}
    edit["review"] = "Duration and audio assembled locally. Compare the result with the source: generated video can change labels, geometry or motion. Visual fidelity has not been automatically verified."
    edit["status"] = "completed"
    edit.pop("error", None)
    write_json(folder / "edit.json", edit)
    await emit("stitch", "local", "FFmpeg", "Source edit ready with original audio. Review the before/after evidence frames.", "completed",
               [{**anchor, "label": f"Result {name} frame"} for name, anchor in edit["edited_anchors"].items()])
    return {"edit": edit}


@app.post("/api/videos/{video_id}/edits/{edit_id}/generate")
async def generate_endpoint(video_id: str, edit_id: str):
    edit = load_edit(video_id, edit_id)
    if edit["status"] == "generating" and JOBS.get(edit.get("job_id"), {}).get("status") in ("queued", "running"):
        raise HTTPException(409, "This edit is already generating.")
    if edit["status"] == "completed":
        async def existing(emit):
            await emit("output", "local", "Saved result", "Reopened the completed edit without new API calls.", "completed")
            return {"edit": edit, "reused": True}
        return start_job("generate", existing, video_id, workflows.build_workflow("generate", CONFIG, video=load_video(video_id), edit=edit))
    config = routing.for_job(CONFIG, "generate", load_video(video_id), edit)
    if not edit["demo"]:
        if edit.get("strategy") == "source_video":
            # A retry resumes its original provider instead of creating another paid job.
            provider = edit.get("generation_provider", config["models"].get("video_edit_provider", "higgsfield"))
            model_field = "fal_video_edit_model" if provider == "fal" else "video_edit_model"
            config["models"]["video_edit_provider"] = provider
            if edit.get("generation_model"):
                config["models"][model_field] = edit["generation_model"]
            if not config["keys"].get(provider):
                label = {"higgsfield": "Higgsfield", "fal": "fal.ai"}.get(provider, provider)
                raise HTTPException(400, f"Add your {label} API key in Models to edit the source video.")
            edit["generation_provider"] = provider
            edit["generation_model"] = config["models"][model_field]
        elif not config["keys"]["openai"] or not config["keys"]["fal"]:
            raise HTTPException(400, "Add OpenAI and fal.ai keys in Models to edit the anchor images and generate video.")
    folder = edit_folder(video_id, edit_id)
    workflow = workflows.build_workflow("generate", config, video=load_video(video_id), edit=edit)
    edit["status"] = "generating"
    write_json(folder / "edit.json", edit)
    async def worker(emit):
        video = load_video(video_id)
        try:
            if edit.get("strategy") == "source_video" and not video["demo"]:
                return await generate_source_edit(video, edit, folder, config, emit)
            if video["demo"]:
                await emit("edit-anchors", "local", "Scripted illustration", "Applying the supported graphics changes to the two demo boundary frames.")
                for name, t in (("start", edit["start"]), ("end", edit["end"] - 1/media.FPS)):
                    await asyncio.to_thread(lambda n=name, stamp=t: demo.scene(stamp, True, edit["instruction"]).save(folder / f"edited-{n}.png"))
                await emit("edit-anchors", "local", "Scripted illustration", "Edited demo anchors rendered without AI.", "completed")
            else:
                instruction = (edit["instruction"] + ". Preserve the exact viewpoint, composition, lighting, subjects, and all unrelated objects. "
                               "This is a boundary frame of a video; make only the requested scene changes. " + edit["prompt"])
                for name, label in (("start", "First frame"), ("end", "Last frame")):
                    path = folder / f"edited-{name}.png"
                    valid = False
                    if path.exists():
                        try:
                            with Image.open(path) as existing:
                                existing.verify()
                            valid = True
                        except (OSError, ValueError):
                            path.unlink(missing_ok=True)
                    if valid:
                        await emit("reuse-anchor", "local", "Saved image", f"Reusing the completed {label.lower()} image; no additional image API call.", "completed")
                        await emit(f"reuse-{name}-anchor", "local", "Saved image", f"{label} reused; image API call skipped.", "completed")
                    else:
                        await providers.edit_image(config, folder / f"{name}.jpg", path, instruction, emit, label)
                # Image models may return a standard canvas. Both boundaries must
                # have identical source dimensions for stable video conditioning.
                for name in ("start", "end"):
                    path = folder / f"edited-{name}.png"
                    with Image.open(path) as generated:
                        resized = generated.convert("RGB").resize((video["width"], video["height"]), Image.Resampling.LANCZOS)
                    resized.save(path)
            edit["edited_anchors"] = {name: {"url": asset_url(video_id, folder / f"edited-{name}.png")} for name in ("start", "end")}
            write_json(folder / "edit.json", edit)
            await emit("conditioning", "local", "Boundary frames", "Using the edited first and last frames to condition the replacement.", "completed",
                       [{**anchor, "label": f"Edited {name} frame"} for name, anchor in edit["edited_anchors"].items()])
            if video["demo"]:
                await emit("generate", "local", "Scripted renderer", "Rendering the illustrated replacement. This is not a generative model result.")
                await asyncio.to_thread(demo.render, folder / "replacement.mp4", edit["start"], edit["end"], edit["instruction"])
                await emit("generate", "local", "Scripted renderer", "Demo replacement rendered.", "completed")
            else:
                replacement = folder / "replacement.mp4"
                valid = False
                if replacement.exists():
                    try:
                        await asyncio.to_thread(media.probe, replacement)
                        valid = True
                    except (media.MediaError, ValueError):
                        replacement.unlink(missing_ok=True)
                if valid:
                    await emit("reuse-replacement", "local", "Saved video", "Reusing the generated replacement; no additional video generation call.", "completed")
                else:
                    await providers.generate_video(config, folder / "edited-start.png", folder / "edited-end.png", replacement,
                                                   edit["prompt"], edit["end"] - edit["start"], emit)
            edit["replacement_url"] = asset_url(video_id, folder / "replacement.mp4")
            await emit("stitch", "local", "FFmpeg", "Matching replacement duration and stitching it between the untouched source segments; retaining original audio.")
            await asyncio.to_thread(media.stitch, directory(video_id) / "video.mp4", folder / "replacement.mp4", folder / "output.mp4", edit["start"], edit["end"])
            edit["output_url"] = asset_url(video_id, folder / "output.mp4")
            edit["status"] = "completed"
            edit.pop("error", None)
            write_json(folder / "edit.json", edit)
            await emit("stitch", "local", "FFmpeg", "Completed the edited video. Original full-clip audio is retained when present.", "completed",
                       [{"url": edit["output_url"], "label": "Edited full video"}])
            return {"edit": edit}
        except BaseException as exc:
            edit["status"] = "failed"
            edit["error"] = "Generation interrupted. Check provider history before retrying." if isinstance(exc, asyncio.CancelledError) else clean_error(exc)
            write_json(folder / "edit.json", edit)
            raise
    # Independent edits only write their own folders; the source is immutable.
    created = start_job("generate", worker, f"{video_id}:{edit_id}", workflow)
    edit["job_id"] = created["job_id"]
    write_json(folder / "edit.json", edit)
    return created


@app.get("/media/{video_id}/{asset_path:path}")
async def get_media(video_id: str, asset_path: str):
    folder = directory(video_id).resolve()
    path = (folder / asset_path).resolve()
    if not path.is_relative_to(folder) or path.suffix not in (".mp4", ".jpg", ".png") or not path.is_file():
        raise HTTPException(404, "Media not found.")
    return FileResponse(path)


app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")
