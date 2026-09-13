import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from interview_copilot.app import create_app

BASE = "http://127.0.0.1:8765"


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name), "overlay-test")
        self.client = TestClient(self.app, base_url=BASE, headers={"Origin": BASE})
        self.client.__enter__()
        self.client.get("/launch?token=overlay-test")
        self.c = self.app.state.controller
        self.c.session["answers"] = [
            {"id": "old", "question": "旧问题", "text": "旧回答", "status": "done", "internal": "private"},
            {"id": "latest", "question": "最新问题", "text": "实时回答", "status": "streaming"},
        ]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_feed_contains_only_selected_answer_and_no_resume_or_keys(self):
        self.c.store.state["profiles"][0]["resume"] = "PRIVATE_RESUME"
        self.c.store.secrets["deepseek_key"] = "PRIVATE_KEY"
        response = self.client.get("/api/overlay/state")
        self.assertEqual(response.json()["answer"]["id"], "latest")
        self.assertNotIn("PRIVATE", response.text)
        self.assertNotIn("profiles", response.text)
        self.assertNotIn("internal", response.text)

    def test_selection_rejects_missing_answer_and_resets_with_new_session(self):
        self.assertEqual(self.client.post("/api/overlay/select", json={"id": "other-session"}).status_code, 404)
        self.client.post("/api/overlay/select", json={"id": "old"}).raise_for_status()
        self.assertEqual(self.client.get("/api/overlay/state").json()["answer"]["id"], "old")
        self.client.post("/api/session/new").raise_for_status()
        self.assertIsNone(self.client.get("/api/overlay/state").json()["answer"])

    def test_open_selects_answer_and_requests_raise_without_disclosing_token(self):
        with patch.object(self.c.overlay, "open") as opened:
            response = self.client.post("/api/overlay/open", json={"answer_id": "old"})
        self.assertEqual(response.json(), {"opened": True})
        opened.assert_called_once_with(8765, "overlay-test", self.c.store.root)
        value = self.client.get("/api/overlay/state").json()
        self.assertEqual(value["answer"]["id"], "old")
        self.assertEqual(value["show_revision"], 1)

    def test_overlay_requires_cookie_and_same_origin(self):
        self.assertEqual(self.client.post("/api/overlay/open", headers={"Origin": "https://other.example"}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/overlay/state").status_code, 401)

    def test_new_accuracy_settings_validate_and_persist(self):
        response = self.client.post("/api/settings", json={"asr_quality": "accurate", "asr_provider": "qwen"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["asr_quality"], "accurate")
        self.assertEqual(self.client.post("/api/settings", json={"asr_quality": "unknown"}).status_code, 422)
        self.assertEqual(response.json()["asr_provider"], "qwen")


if __name__ == "__main__":
    unittest.main()
