"""Custom answer preferences persist per profile and reach the model intact."""

import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from interview_copilot.app import create_app
from interview_copilot import intelligence as ai
from interview_copilot.storage import Store


BASE = "http://127.0.0.1:8765"


class CustomPromptPersistenceTests(unittest.TestCase):
    def test_api_roundtrip_profile_isolation_clear_and_length_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = create_app(root, "custom-prompt-test-token")
            with TestClient(app, base_url=BASE, headers={"Origin": BASE}) as client:
                client.get("/launch?token=custom-prompt-test-token")
                profile_id = client.get("/api/state").json()["active_profile_id"]
                prompt = "自然口语。" * 800
                response = client.post("/api/profiles", json={"id": profile_id, "custom_prompt": prompt})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["custom_prompt"], prompt)
                client.post("/api/profiles", json={"id": profile_id, "role": "售前顾问"})
                self.assertEqual(Store(root).active_profile()["custom_prompt"], prompt)
                other = client.post("/api/profiles", json={"name": "另一场面试"}).json()
                self.assertEqual(other["custom_prompt"], "")
                too_long = client.post("/api/profiles", json={"id": profile_id, "custom_prompt": prompt + "字"})
                self.assertEqual(too_long.status_code, 422)
                self.assertEqual(Store(root).active_profile()["custom_prompt"], prompt)
                cleared = client.post("/api/profiles", json={"id": profile_id, "custom_prompt": ""})
                self.assertEqual(cleared.json()["custom_prompt"], "")
                self.assertEqual(Store(root).active_profile()["custom_prompt"], "")

    def test_legacy_profile_without_field_loads_and_keeps_existing_material(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            profile = store.save_profile({"id": store.active_profile()["id"], "resume": "真实的项目经历"})
            legacy = json.loads((root / "state.json").read_text(encoding="utf-8"))
            legacy["profiles"][0].pop("custom_prompt")
            (root / "state.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            reloaded = Store(root)
            self.assertEqual(reloaded.active_profile()["custom_prompt"], "")
            reloaded.save_profile({"id": profile["id"], "custom_prompt": "用短句表达。"})
            self.assertEqual(Store(root).active_profile()["resume"], "真实的项目经历")
            self.assertEqual(Store(root).active_profile()["custom_prompt"], "用短句表达。")


class CustomPromptMessagesTests(unittest.TestCase):
    def test_full_preferences_reach_all_modes_separate_from_factual_evidence(self):
        prompt = "用自然口语，回答不分点。\n" + "请完整保留这段表达偏好。" * 320
        self.assertLessEqual(len(prompt), ai.MAX_CUSTOM_PROMPT)
        for mode in ("answer", "prepare", "review"):
            with self.subTest(mode=mode):
                messages, sources = ai.build_messages(
                    {"custom_prompt": prompt}, "请介绍一下自己。", [],
                    {"answer_language": "en", "answer_style": "star"}, mode,
                )
                request = json.loads(messages[-1]["content"])
                self.assertEqual(request["自定义回答要求"], prompt)
                self.assertEqual(request["相关参考片段"], [])
                self.assertEqual(sources, [])
                self.assertNotIn(prompt, messages[1]["content"])
                self.assertIn("English", request["语言"])
                self.assertIn("优先遵循自定义回答要求", messages[0]["content"])
                self.assertIn("不能编造候选人", messages[0]["content"])
                self.assertIn("回答语言仍以本次", messages[0]["content"])

    def test_oversized_internal_value_is_bounded_even_without_api_validation(self):
        prompt = "要求" * ai.MAX_CUSTOM_PROMPT
        messages, _ = ai.build_messages({"custom_prompt": prompt}, "请介绍自己", [], {})
        self.assertEqual(json.loads(messages[-1]["content"])["自定义回答要求"], prompt[:ai.MAX_CUSTOM_PROMPT])

    def test_empty_or_invalid_custom_prompt_keeps_existing_message_behavior(self):
        expected = ai.build_messages({"resume": "已有简历"}, "请介绍自己", [], {})
        for empty in ("", " \n\t ", None, 123, []):
            with self.subTest(value=empty):
                self.assertEqual(
                    ai.build_messages({"resume": "已有简历", "custom_prompt": empty}, "请介绍自己", [], {}),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
