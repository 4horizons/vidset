"""Routing tests use only explicit fake config, independent of local .env keys."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import fal_models, main, providers, workflows


def config_with(**keys):
    config = {"keys": {name: "" for name in ("openai", "gemini", "anthropic", "fal", "higgsfield")},
              "models": {"vision_provider": "gemini", "vision_model": "gemini-2.5-flash",
                         "chat_provider": "openai", "chat_model": "gpt-4.1-mini",
                         "video_edit_provider": "higgsfield", "video_edit_model": "bytedance/seedance-2.5/video-edit",
                         "fal_video_edit_model": fal_models.SOURCE_MODEL}}
    config["keys"].update(keys)
    return config


class ModelSelectionTests(unittest.TestCase):
    def test_openai_and_higgsfield_keys_enable_both_llm_roles_and_source_edits(self):
        config = config_with(openai="offline-openai", higgsfield="offline-id:offline-secret")
        main.configure_available_models(config)
        self.assertEqual(providers._selection(config), ("openai", "gpt-4.1-mini"))
        self.assertEqual(providers._reasoning_selection(config), ("openai", "gpt-4.1-mini"))
        self.assertEqual(config["models"]["video_edit_provider"], "higgsfield")

    def test_fal_only_selects_distinct_reasoning_and_video_capable_vision_models(self):
        config = config_with(fal="offline-fal")
        main.configure_available_models(config)
        self.assertEqual(providers._selection(config), ("fal", fal_models.DEFAULT_VISION_MODEL))
        self.assertEqual(providers._reasoning_selection(config), ("fal", fal_models.DEFAULT_CHAT_MODEL))
        self.assertEqual(config["models"]["video_edit_provider"], "fal")
        chat = workflows.build_workflow("chat", config, {"analysis": {"summary": "Observed can"}})
        rows = {row["id"]: row for row in chat["steps"]}
        self.assertEqual(rows["visual_verification"]["model"], fal_models.DEFAULT_VISION_MODEL)
        self.assertEqual(rows["answer_synthesis"]["model"], fal_models.DEFAULT_CHAT_MODEL)

    def test_explicit_selection_is_preserved_even_when_its_key_is_missing(self):
        config = config_with(fal="offline-fal")
        config["models"].update(vision_provider="anthropic", vision_model="chosen-model",
                                chat_provider="openai", chat_model="chosen-reasoner", video_edit_provider="higgsfield")
        before = copy.deepcopy(config)
        main.configure_available_models(config, {"vision_provider": "anthropic", "chat_provider": "openai", "video_edit_provider": "higgsfield"})
        self.assertEqual(config, before)

    def test_working_custom_selection_and_no_key_configuration_remain_unchanged(self):
        config = config_with(gemini="offline-gemini", openai="offline-openai", higgsfield="offline-id:offline-secret")
        config["models"].update(vision_model="gemini-custom", chat_model="openai-custom")
        for candidate in (config, config_with()):
            before = copy.deepcopy(candidate)
            main.configure_available_models(candidate)
            self.assertEqual(candidate, before)

    def test_fal_source_manifest_names_actual_provider_and_preserves_cache_mapping(self):
        config = config_with(fal="offline-fal")
        main.configure_available_models(config)
        manifest = workflows.build_workflow("generate", config, {"demo": False}, {"strategy": "source_video"})
        self.assertEqual([row["id"] for row in manifest["steps"]], ["source_clip", "source_upload", "source_video_edit", "stitch"])
        for row in manifest["steps"][1:3]:
            self.assertEqual((row["provider"], row["model"]), ("fal", fal_models.SOURCE_MODEL))
            self.assertIn("reuse-replacement", row["skip_on"])
        self.assertIn("reuse-source-upload", manifest["steps"][1]["skip_on"])
        self.assertNotIn("offline-fal", json.dumps(manifest))


class FalDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_adapter_delegates_arguments_without_direct_provider_call(self):
        config = config_with(fal="offline-fal")
        frames = [{"timestamp": 1.5, "path": Path("evidence.jpg")}]
        answer = {"answer": "A visible can."}
        with patch.object(fal_models, "call_json", new=AsyncMock(return_value=answer)) as route, patch.object(providers.httpx, "AsyncClient", side_effect=AssertionError("No direct-provider network client expected")):
            result = await providers._call_json(config, "fal", fal_models.DEFAULT_VISION_MODEL, "Inspect evidence.", frames=frames)
        self.assertEqual(result, answer)
        route.assert_awaited_once_with(config, fal_models.DEFAULT_VISION_MODEL, "Inspect evidence.", frames=frames, video_path=None)

    async def test_fal_analysis_uses_complete_video_not_sparse_stills(self):
        config = config_with(fal="offline-fal")
        main.configure_available_models(config)
        video = Path("offline-proxy.mp4")
        async def emit(*args, **kwargs):
            pass
        with patch.object(fal_models, "call_json", new=AsyncMock(return_value={"summary": "The can rotates.", "events": []})) as route:
            result = await providers.analyze_video(config, video, [{"timestamp": 1, "path": "unused.jpg"}], 5.0, emit)
        self.assertEqual(route.await_args.kwargs, {"frames": [], "video_path": video})
        self.assertEqual((result["provider"], result["model"]), ("fal", fal_models.DEFAULT_VISION_MODEL))
        self.assertIn("compressed video proxy", result["limitations"])


if __name__ == "__main__":
    unittest.main()
