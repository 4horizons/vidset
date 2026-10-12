"""Small, credential-free manifests for the work a local job will actually do.

Rows describe expected calls, not fabricated progress. The UI advances rows
from real emitted stages and must use event provider/model values when present.
Only completed events satisfy complete_on; skip_on identifies cached work.
Unmapped informational events remain visible in the activity log.
"""
from __future__ import annotations

from .providers import DEFAULTS


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _selection(config, purpose="vision"):
    """Mirror provider selection without requiring a key to build the UI."""
    models = _mapping(config.get("models"))
    default = "gemini" if purpose == "vision" else "openai"
    provider = models.get(f"{purpose}_provider", default)
    if provider not in DEFAULTS:
        provider = default
    model = models.get(f"{purpose}_model")
    return provider, model if isinstance(model, str) and model else DEFAULTS[provider]


def _reasoning_selection(config):
    models = _mapping(config.get("models"))
    provider = models.get("chat_provider", "openai")
    if isinstance(provider, str) and _mapping(config.get("keys")).get(provider):
        return _selection(config, "chat")
    return _selection(config)


def _step(identifier, title, provider="local", model="FFmpeg", *, stages=None, complete_on=None, skip_on=None):
    stages = list(stages) if stages is not None else [identifier]
    result = {"id": identifier, "title": title, "provider": provider, "model": model,
              "stages": stages, "complete_on": list(complete_on) if complete_on is not None else stages.copy()}
    if skip_on:
        result["skip_on"] = list(skip_on)
    return result


def _model(config, name, default):
    value = _mapping(config.get("models")).get(name)
    return value if isinstance(value, str) and value else default


def _analysis_steps(config, demo):
    if demo:
        return [_step("understand", "Read the sample timeline", model="Scripted demo")]
    provider, model = _selection(config)
    return [
        _step("proxy", "Prepare video for analysis"),
        _step("video_analysis", "Analyze the video", provider, model),
    ]


def build_workflow(kind, config, video=None, edit=None):
    """Return a JSON-safe workflow with no provider keys or copied media data.

    Pass the video snapshot from job creation so missing analysis can be shown.
    For an existing demo, pass its metadata; for a new demo, leave video absent.
    Pass the original edit snapshot before changing its status to generating.
    """
    config, video, edit = _mapping(config), _mapping(video), _mapping(edit)
    demo = bool(video.get("demo") or edit.get("demo"))
    steps = []
    titles = {"upload": "Prepare your video", "demo": "Open the sample",
              "analyze": "Analyze the video", "chat": "Answer with evidence",
              "plan": "Plan the edit", "generate": "Create the replacement"}

    if kind == "upload" or (kind == "demo" and not video):
        steps = [_step("prepare", "Prepare the sample" if kind == "demo" else "Prepare the video"),
                 _step("frames", "Extract evidence frames")]
    elif kind == "demo":
        steps = [_step("demo", "Open the saved sample", model="Scripted demo")]
    elif kind == "analyze":
        steps = _analysis_steps(config, demo)
    elif kind in {"chat", "plan"}:
        if not video.get("analysis"):
            steps.extend(_analysis_steps(config, demo))
        if kind == "chat":
            if demo:
                steps.append(_step("answer", "Answer from the sample", model="Scripted demo"))
            else:
                vision = _selection(config)
                reasoning = _reasoning_selection(config)
                steps.extend([
                    _step("retrieve", "Find relevant evidence frames"),
                    _step("visual_verification", "Inspect the evidence", *vision),
                ])
                # answer_question emits an info-only synthesis event when the
                # same model answers. That is not another model call or row.
                if reasoning != vision:
                    steps.append(_step("answer_synthesis", "Compose the answer", *reasoning))
            steps.append(_step("evidence", "Attach timestamped evidence"))
        else:
            steps.append(_step("plan", "Plan the sample edit", model="Scripted demo") if demo else
                         _step("edit_planning", "Locate the moment and plan the edit", *_reasoning_selection(config)))
            steps.append(_step("anchors", "Extract boundary evidence frames"))
    elif kind == "generate":
        if edit.get("status") == "completed":
            steps = [_step("output", "Open the saved result", model="Saved result")]
        elif demo:
            steps = [
                _step("edit-anchors", "Illustrate the sample changes", model="Scripted illustration"),
                _step("conditioning", "Prepare the boundary frames", model="Boundary frames"),
                _step("generate", "Render the sample replacement", model="Scripted renderer"),
                _step("stitch", "Assemble the edited video"),
            ]
        elif edit.get("strategy") == "source_video":
            provider = config.get("models", {}).get("video_edit_provider", "higgsfield")
            model = (_model(config, "fal_video_edit_model", "bytedance/seedance-2.5/reference-to-video") if provider == "fal" else
                     _model(config, "video_edit_model", "bytedance/seedance-2.5/video-edit"))
            steps = [
                _step("source_clip", "Extract the source interval"),
                _step("source_upload", "Upload the source clip", provider, model,
                      skip_on=["reuse-source-upload", "reuse-replacement"]),
                _step("source_video_edit", "Edit the source video", provider, model,
                      skip_on=["reuse-replacement"]),
                _step("stitch", "Restore audio and assemble the video"),
            ]
        else:
            image_model = _model(config, "image_model", "gpt-image-1.5")
            video_model = _model(config, "video_model", "fal-ai/kling-video/o1/image-to-video")
            steps = [
                _step("edit_start_anchor", "Edit the first frame", "openai", image_model,
                      stages=["edit_First frame_anchor"], skip_on=["reuse-start-anchor"]),
                _step("edit_end_anchor", "Edit the last frame", "openai", image_model,
                      stages=["edit_Last frame_anchor"], skip_on=["reuse-end-anchor"]),
                _step("conditioning", "Prepare the edited boundary frames", model="Boundary frames"),
                _step("video_generation", "Generate the replacement clip", "fal", video_model,
                      skip_on=["reuse-replacement"]),
                _step("stitch", "Assemble the video with original audio"),
            ]
    result = {"title": titles.get(kind, "Local workflow"), "steps": steps}
    if config.get("_routing"):
        result["routing"] = config["_routing"]
    return result
