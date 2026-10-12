"""Workflow manifests describe real conditional calls without exposing keys."""
import copy
import json
import unittest

from app import providers
from app.workflows import build_workflow


CONFIG = {"keys": {"gemini": "secret-vision", "openai": "secret-chat", "anthropic": "", "higgsfield": "private-id:private-secret", "fal": "secret-video"},
          "models": {"vision_provider": "gemini", "vision_model": "gemini-2.5-flash",
                     "chat_provider": "openai", "chat_model": "gpt-4.1-mini",
                     "image_model": "gpt-image-1.5", "video_model": "fal-ai/kling-video/o1/image-to-video",
                     "video_edit_model": "bytedance/seedance-2.5/video-edit"}}
VIDEO = {"id": "sample", "demo": False, "analysis": {"summary": "Private content must not enter a workflow."}}


def ids(workflow):
    return [step["id"] for step in workflow["steps"]]


class WorkflowTests(unittest.TestCase):
    def test_upload_and_new_or_saved_demo_reflect_different_actual_work(self):
        self.assertEqual(ids(build_workflow("upload", CONFIG)), ["prepare", "frames"])
        self.assertEqual(ids(build_workflow("demo", CONFIG)), ["prepare", "frames"])
        saved = build_workflow("demo", CONFIG, {"id": "saved", "demo": True, "frames": [{}]})
        self.assertEqual(ids(saved), ["demo"])
        self.assertEqual(saved["steps"][0]["model"], "Scripted demo")

    def test_chat_adds_analysis_only_when_needed_and_a_real_second_call_only(self):
        ready = build_workflow("chat", CONFIG, VIDEO)
        self.assertEqual(ids(ready), ["retrieve", "visual_verification", "answer_synthesis", "evidence"])
        new = build_workflow("chat", CONFIG, {"demo": False})
        self.assertEqual(ids(new), ["proxy", "video_analysis", *ids(ready)])
        self.assertEqual(new["steps"][1]["provider"], "gemini")

        for same_model, remove_chat_key in [(True, False), (False, True)]:
            config = copy.deepcopy(CONFIG)
            if same_model:
                config["models"].update(chat_provider="gemini", chat_model="gemini-2.5-flash")
            if remove_chat_key:
                config["keys"]["openai"] = ""
            self.assertEqual(ids(build_workflow("chat", config, VIDEO)), ["retrieve", "visual_verification", "evidence"])

    def test_same_provider_with_distinct_model_still_gets_a_real_synthesis_row(self):
        config = copy.deepcopy(CONFIG)
        config["models"].update(chat_provider="gemini", chat_model="gemini-reasoning-model")
        row = build_workflow("chat", config, VIDEO)["steps"][2]
        self.assertEqual((row["id"], row["provider"], row["model"]),
                         ("answer_synthesis", "gemini", "gemini-reasoning-model"))

    def test_reasoning_model_matches_provider_fallback_for_planning(self):
        for chat_provider, has_key in [("openai", True), ("anthropic", False), ("anthropic", True)]:
            config = copy.deepcopy(CONFIG)
            config["models"].update(chat_provider=chat_provider, chat_model=f"{chat_provider}-planner")
            config["keys"][chat_provider] = "present" if has_key else ""
            expected = providers._reasoning_selection(config)
            row = build_workflow("plan", config, VIDEO)["steps"][0]
            self.assertEqual((row["provider"], row["model"]), expected)
            self.assertEqual(row["stages"], ["edit_planning"])
        self.assertEqual(ids(build_workflow("plan", CONFIG, {"demo": False})),
                         ["proxy", "video_analysis", "edit_planning", "anchors"])

    def test_demo_never_claims_ai_model_calls(self):
        video = {"demo": True}
        expected = {"analyze": ["understand"], "chat": ["understand", "answer", "evidence"],
                    "plan": ["understand", "plan", "anchors"],
                    "generate": ["edit-anchors", "conditioning", "generate", "stitch"]}
        for kind, stages in expected.items():
            workflow = build_workflow(kind, CONFIG, video)
            self.assertEqual(ids(workflow), stages)
            self.assertTrue(all(row["provider"] == "local" for row in workflow["steps"]))

    def test_source_workflow_does_not_claim_anchor_generation_and_maps_reuse(self):
        result = build_workflow("generate", CONFIG, VIDEO, {"strategy": "source_video"})
        self.assertEqual(ids(result), ["source_clip", "source_upload", "source_video_edit", "stitch"])
        upload, generate = result["steps"][1:3]
        self.assertEqual(upload["skip_on"], ["reuse-source-upload", "reuse-replacement"])
        self.assertEqual(generate["skip_on"], ["reuse-replacement"])
        self.assertEqual(generate["complete_on"], ["source_video_edit"])
        self.assertEqual((generate["provider"], generate["model"]), ("higgsfield", "bytedance/seedance-2.5/video-edit"))
        self.assertNotIn("openai", [row["provider"] for row in result["steps"]])

    def test_anchor_workflow_uses_exact_emitted_stage_names_and_separate_reuse(self):
        workflow = build_workflow("generate", CONFIG, VIDEO, {"strategy": "anchors"})
        first, last, conditioning, generate, stitch = workflow["steps"]
        self.assertEqual(first["stages"], ["edit_First frame_anchor"])
        self.assertEqual(last["stages"], ["edit_Last frame_anchor"])
        self.assertEqual(first["skip_on"], ["reuse-start-anchor"])
        self.assertEqual(last["skip_on"], ["reuse-end-anchor"])
        self.assertEqual(generate["skip_on"], ["reuse-replacement"])
        self.assertEqual(generate["provider"], "fal")
        self.assertEqual(stitch["provider"], "local")

    def test_completed_edits_reopen_without_upload_or_generation_rows(self):
        for strategy in ("anchors", "source_video"):
            result = build_workflow("generate", CONFIG, VIDEO, {"strategy": strategy, "status": "completed"})
            self.assertEqual(ids(result), ["output"])
            self.assertEqual(result["steps"][0]["provider"], "local")

    def test_no_keys_required_and_manifests_are_bounded_unique_and_secret_free(self):
        before = copy.deepcopy(CONFIG)
        for config in (CONFIG, {}, {"keys": {}, "models": {}}, None):
            for kind in ("upload", "demo", "analyze", "chat", "plan", "generate", "unknown"):
                result = build_workflow(kind, config)
                self.assertLessEqual(len(result["steps"]), 6)
                self.assertEqual(len(ids(result)), len(set(ids(result))))
                serialized = json.dumps(result)
                for key in CONFIG["keys"].values():
                    if key:
                        self.assertNotIn(key, serialized)
                self.assertNotIn("Private content", serialized)
                for row in result["steps"]:
                    self.assertTrue(row["stages"] and row["complete_on"])
                    self.assertTrue(set(row["complete_on"]).issubset(row["stages"]))
        self.assertEqual(CONFIG, before)


if __name__ == "__main__":
    unittest.main()
