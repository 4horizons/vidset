"""Real local FFmpeg contract tests; no model calls or external media."""
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app import media


pytestmark = pytest.mark.skipif(not media.available(), reason="FFmpeg and ffprobe required")


def ffmpeg(*args):
    return subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)],
                          check=True, capture_output=True, timeout=30).stdout


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    folder = tmp_path_factory.mktemp("media-contract")
    source = folder / "source.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=160x96:r=24:d=6",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source)
    replacements = {}
    for seconds in (1, 4):
        patch = folder / f"patch-{seconds}.mp4"
        # The wrong tone is deliberate: replacement audio must never leak into output.
        ffmpeg("-f", "lavfi", "-i", f"color=c=blue:s=96x64:r=24:d={seconds}",
               "-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=48000:duration={seconds}",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", patch)
        replacements[seconds] = patch
    silent = folder / "silent.mp4"
    ffmpeg("-i", source, "-map", "0:v:0", "-c:v", "copy", "-an", silent)
    return source, replacements, silent


def decoded_audio(path):
    return ffmpeg("-i", path, "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1")


def pixel_at(path, timestamp):
    raw = ffmpeg("-ss", timestamp, "-i", path, "-frames:v", "1",
                 "-vf", "crop=2:2:80:48", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1")
    assert len(raw) == 12
    return tuple(raw[:3])


@pytest.mark.parametrize("start,end", [(0, 2), (2, 4), (4, 6)])
@pytest.mark.parametrize("generated_seconds", [1, 4])
def test_stitch_preserves_duration_audio_and_unselected_pictures(clips, tmp_path, start, end, generated_seconds):
    source, patches, _ = clips
    output = tmp_path / "output.mp4"
    meta = media.stitch(source, patches[generated_seconds], output, start, end)
    assert abs(meta["duration"] - media.probe(source)["duration"]) <= 1 / media.FPS
    assert (meta["width"], meta["height"], meta["fps"]) == (160, 96, 24)
    assert meta["has_audio"] is True
    # Byte equality of decoded audio confirms timing, content, and duration.
    assert decoded_audio(output) == decoded_audio(source)
    for at in (0.25, 1.75, 2.25, 3.75, 4.25, 5.75):
        red, green, blue = pixel_at(output, at)
        if start <= at < end:
            assert blue > 220 and red < 30, (at, red, green, blue)
        else:
            assert red > 220 and blue < 30, (at, red, green, blue)


def test_stitch_source_without_audio_stays_silent(clips, tmp_path):
    _, patches, silent = clips
    result = media.stitch(silent, patches[1], tmp_path / "silent-result.mp4", 2, 4)
    assert result["has_audio"] is False
    assert result["duration"] == pytest.approx(6, abs=1 / media.FPS)


@pytest.mark.parametrize("start,end,duration", [
    (-0.1, 2, 6), (2, 2, 6), (3, 2, 6), (0, 6.5, 6),
    (0, 0.5, 6), (0, 11, 20), (float("nan"), 2, 6),
    (0, float("inf"), 6), (0, 2, float("nan")),
])
def test_invalid_edit_ranges_rejected(start, end, duration):
    with pytest.raises(media.MediaError):
        media.edit_range(start, end, duration)


def test_frame_extraction_clamps_to_real_video_bounds(clips, tmp_path):
    source, _, _ = clips
    duration = media.probe(source)["duration"]
    for i, requested in enumerate((-1, 0, 0.7, duration, duration + 8)):
        picture = tmp_path / f"frame-{i}.jpg"
        actual = media.frame(source, requested, picture, duration)
        assert 0 <= actual <= duration - 1 / media.FPS + 0.001
        with Image.open(picture) as image:
            assert image.size == (160, 96)
    for value in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(media.MediaError):
            media.frame_time(value, duration)


def test_exported_frame_timestamps_round_trip_without_drifting(clips, tmp_path):
    source, _, _ = clips
    duration = media.probe(source)["duration"]
    # A stored evidence timestamp is sent back through extraction during chat.
    first = media.frame(source, 1.1, tmp_path / "first.jpg", duration)
    second = media.frame(source, first, tmp_path / "second.jpg", duration)
    assert first == second
    assert (tmp_path / "first.jpg").read_bytes() == (tmp_path / "second.jpg").read_bytes()
    for index in range(round(duration * media.FPS)):
        stored_timestamp = round(index / media.FPS, 3)
        assert media.frame_time(stored_timestamp, duration) == pytest.approx(index / media.FPS, abs=1e-6)


def test_contact_sheet_timestamps_are_ordered_unique_and_reextractable():
    times = media.sample_times(12, count=12)
    assert len(times) == 12
    assert times == sorted(set(times))
    assert times[0] == 0
    assert times[-1] == pytest.approx(12 - 1 / media.FPS, abs=0.001)
    for timestamp in times:
        assert abs(media.frame_time(timestamp, 12) - timestamp) <= 0.001


def test_short_audio_tail_does_not_break_uploaded_contact_sheet(tmp_path):
    source, normalized = tmp_path / "tail.mp4", tmp_path / "normalized.mp4"
    # Container/audio duration can be slightly longer than the last visual frame.
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=160x96:r=24:d=2",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2.04",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", source)
    meta = media.normalize(source, normalized)
    assert meta["has_audio"] is True
    for i, at in enumerate(media.sample_times(meta["duration"])):
        image = tmp_path / f"tail-frame-{i}.jpg"
        actual = media.frame(normalized, at, image, meta["duration"])
        assert image.is_file()
        assert actual <= meta["duration"]
