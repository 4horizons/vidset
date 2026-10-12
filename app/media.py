"""Bounded local video operations. No URL ingestion, shell interpolation or cloud storage."""
import json
import math
import shutil
import subprocess
from pathlib import Path

FPS = 24
MAX_SECONDS = 120
MAX_BYTES = 128 * 1024 * 1024
FORMATS = "mov,matroska,webm,avi"


class MediaError(ValueError):
    pass


def available():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def run(args, timeout=240):
    try:
        return subprocess.run(args, capture_output=True, check=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise MediaError("Install FFmpeg (including ffprobe) and restart the app.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Video processing timed out. Try a shorter or smaller clip.") from exc
    except subprocess.CalledProcessError as exc:
        # Do not expose local paths or ffmpeg's untrusted metadata in API errors.
        raise MediaError("FFmpeg could not process this clip. Try an MP4, MOV, or WebM file.") from exc


def probe(path: Path):
    result = run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                  "-format_whitelist", FORMATS, "-show_format", "-show_streams",
                  "-of", "json", str(path)], timeout=30)
    raw = json.loads(result.stdout)
    stream = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    if not stream:
        raise MediaError("This file has no video stream.")
    # Container duration can include a longer audio tail (even AAC padding).
    # All timestamps, cuts and last-frame clamps follow the visual timeline.
    duration = float(stream.get("duration") or raw.get("format", {}).get("duration") or 0)
    width, height = int(stream["width"]), int(stream["height"])
    if not math.isfinite(duration) or not 0.2 <= duration <= MAX_SECONDS + 0.05:
        raise MediaError(f"Upload a video between 0.2 and {MAX_SECONDS} seconds long.")
    if not 16 <= width <= 8192 or not 16 <= height <= 8192:
        raise MediaError("Video dimensions must be between 16 and 8192 pixels.")
    rate = stream.get("avg_frame_rate", "24/1").split("/")
    fps = float(rate[0]) / max(float(rate[-1]), 1)
    return {"duration": round(duration, 4), "width": width, "height": height,
            "fps": round(fps, 3), "has_audio": any(s.get("codec_type") == "audio" for s in raw["streams"])}


def normalize(source: Path, destination: Path):
    meta = probe(source)
    run(["ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
         "-format_whitelist", FORMATS, "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
         "-vf", "scale='min(iw,if(gte(iw,ih),1280,720))':'min(ih,if(gte(iw,ih),720,1280))':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
         "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-t", str(meta["duration"]),
         "-movflags", "+faststart", str(destination)])
    return probe(destination)


def make_proxy(source: Path, destination: Path):
    if not destination.exists():
        run(["ffmpeg", "-v", "error", "-y", "-i", str(source),
             "-vf", "scale='min(640,iw)':-2", "-r", "12", "-c:v", "libx264", "-preset", "veryfast",
             "-b:v", "600k", "-maxrate", "700k", "-bufsize", "1200k", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(destination)])
    if destination.stat().st_size > 14 * 1024 * 1024:
        raise MediaError("Analysis proxy is too large. Please use a shorter video.")
    return destination


def frame_time(timestamp: float, duration: float):
    if not math.isfinite(timestamp):
        raise MediaError("Frame timestamp must be a finite number.")
    # Public timestamps are rounded to milliseconds. Nearest-frame quantization
    # makes extraction idempotent when a returned timestamp is requested again.
    return max(0, min(round(timestamp * FPS) / FPS, max(0, duration - 1 / FPS)))


def frame(source: Path, timestamp: float, destination: Path, duration: float):
    timestamp = frame_time(timestamp, duration)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        run(["ffmpeg", "-v", "error", "-y", "-ss", f"{timestamp:.6f}", "-i", str(source),
             "-frames:v", "1", "-q:v", "2", str(destination)], timeout=30)
    if not destination.exists():
        raise MediaError("Could not extract an evidence frame.")
    return round(timestamp, 3)


def sample_times(duration: float, count=12, start=0.0, end=None):
    end = duration if end is None else min(end, duration)
    last = max(start, end - 1 / FPS)
    return sorted({round(frame_time(start + (last - start) * i / max(count - 1, 1), duration), 3) for i in range(count)})


def edit_range(start: float, end: float, duration: float, minimum=1, maximum=10):
    if not all(math.isfinite(x) for x in (start, end, duration)):
        raise MediaError("Edit timestamps must be finite numbers.")
    if start < 0 or end > duration + 0.02 or end <= start:
        raise MediaError("Choose a range inside the video, with the end after the start.")
    start = round(start * FPS) / FPS
    end = min(round(end * FPS) / FPS, duration)
    if not minimum - 0.00001 <= end - start <= maximum + 0.00001:
        raise MediaError(f"Choose a segment between {minimum} and {maximum} seconds long.")
    return round(start, 6), round(end, 6)


def stitch(source: Path, replacement: Path, output: Path, start: float, end: float, maximum=10):
    """Retime generated segment to selection, concatenate pictures, preserve original audio."""
    meta = probe(source)
    start, end = edit_range(start, end, meta["duration"], maximum=maximum)
    generated = probe(replacement)
    length = end - start
    ratio = length / generated["duration"]
    filters, clips = [], []
    if start > 0:
        filters.append(f"[0:v]trim=start=0:end={start},setpts=PTS-STARTPTS,fps={FPS},setsar=1[before]")
        clips.append("[before]")
    filters.append(f"[1:v]setpts={ratio:.9f}*(PTS-STARTPTS),fps={FPS},"
                   f"scale={meta['width']}:{meta['height']}:force_original_aspect_ratio=decrease,"
                   f"pad={meta['width']}:{meta['height']}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                   f"tpad=stop_mode=clone:stop_duration=1,trim=duration={length}[patch]")
    clips.append("[patch]")
    if end < meta["duration"] - 0.02:
        filters.append(f"[0:v]trim=start={end},setpts=PTS-STARTPTS,fps={FPS},setsar=1[after]")
        clips.append("[after]")
    filters.append(f"{''.join(clips)}concat=n={len(clips)}:v=1:a=0[out]")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-i", str(replacement),
         "-filter_complex", ";".join(filters), "-map", "[out]", "-map", "0:a:0?", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy",
         "-t", str(meta["duration"]), "-movflags", "+faststart", str(output)])
    return probe(output)


def prepare_edit_source(source: Path, destination: Path, start: float, end: float):
    """Make a silent, upright source segment for the video editor; keep sound local."""
    meta = probe(source)
    start, end = edit_range(start, end, meta["duration"], minimum=4, maximum=30)
    run(["ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
         "-format_whitelist", FORMATS, "-i", str(source), "-ss", str(start),
         "-t", str(end-start), "-map", "0:v:0", "-an",
         "-vf", "scale='min(iw,if(gte(iw,ih),1280,720))':'min(ih,if(gte(iw,ih),720,1280))':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
         "-r", str(FPS), "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination)])
    return probe(destination)
