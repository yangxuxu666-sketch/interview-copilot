"""Independent regression checks for lifecycle and profile isolation."""

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from interview_copilot import intelligence
from interview_copilot.app import Controller, create_app
from interview_copilot.storage import Store


class ReviewAudio:
    """A deterministic audio worker with controllable startup/stop timing."""

    def __init__(self, data_dir, emit, on_transcript):
        self.emit = emit
        self.fail_on_start = False
        self.stop_entered = threading.Event()
        self.stop_release = threading.Event()
        self.stop_release.set()
        self._status = {"state": "idle", "listening": False}

    def configure(self, settings, secrets):
        pass

    def warmup(self):
        pass

    def status(self):
        return self._status.copy()

    def start(self, device_id=None):
        if self.fail_on_start:
            self._status = {"state": "error", "listening": False, "message": "模型加载失败"}
            self.emit({"type": "status", "component": "audio", **self._status})
            # The controller's event loop receives the failure before start's
            # worker future returns, just like a fast ASR dependency failure.
            time.sleep(0.05)
        else:
            self._status = {"state": "listening", "listening": True}
            self.emit({"type": "status", "component": "audio", **self._status})

    def stop(self):
        self.stop_entered.set()
        if not self.stop_release.wait(2):
            raise RuntimeError("Test audio stop timed out")
        self._status = {"state": "idle", "listening": False}

    def close(self):
        self.stop_release.set()


class IntegrationReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="interview-review-")
        self.audio_patch = patch("interview_copilot.audio.AudioService", ReviewAudio)
        self.audio_patch.start()
        self.app = create_app(Path(self.tmp.name), "review-test-token", 8765)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.controller = self.app.state.controller
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://127.0.0.1:8765",
            headers={"origin": "http://127.0.0.1:8765"},
            cookies={"interview_session": "review-test-token"},
        )

    async def asyncTearDown(self):
        self.controller.audio.stop_release.set()
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.audio_patch.stop()
        self.tmp.cleanup()

    async def test_connection_failure_returns_curated_json_message(self):
        with patch.object(intelligence, "test_connection", AsyncMock(side_effect=intelligence.IntelligenceError("测试密钥验证失败"))):
            response = await self.client.post("/api/settings/test")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"], "测试密钥验证失败")

    async def test_recovery_does_not_attach_old_session_to_new_active_profile(self):
        c = self.controller
        c.session["answers"].append({"id": "old-answer", "question": "公司 A 问题", "text": "公司 A 历史", "status": "done"})
        c.store.save_session(c.session)
        next_profile = c.store.save_profile({"name": "公司 B 面试"})
        # Simulate process termination after state.json was switched but before
        # reset() persisted the new profile's session.
        c.store.activate(next_profile["id"])
        recovered = Controller(Store(Path(self.tmp.name)))
        try:
            self.assertEqual(recovered.session["profile_id"], next_profile["id"])
            self.assertEqual(recovered.session["answers"], [])
            self.assertTrue(recovered.store.list_sessions(), "Interrupted previous session should remain available in archives")
        finally:
            await recovered.close()

    async def test_failed_start_cannot_overwrite_audio_error_with_listening(self):
        self.controller.audio.fail_on_start = True
        response = await self.client.post("/api/audio/start", json={"device_id": None})
        state = (await self.client.get("/api/state")).json()
        self.assertFalse(state["session"]["listening"], response.text)
        self.assertFalse(state["capabilities"]["audio_status"]["listening"])

    async def test_model_change_cleanup_keeps_health_responsive(self):
        for endpoint in ("/api/audio/start", "/api/audio/warmup"):
            with self.subTest(endpoint=endpoint):
                entered = threading.Event()
                release = threading.Event()

                def configure(settings, secrets):
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("Test model cleanup timed out")

                with patch.object(self.controller.audio, "configure", side_effect=configure):
                    changing = asyncio.create_task(self.client.post(endpoint, json={"device_id": None}))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                        response = await asyncio.wait_for(self.client.get("/health"), 0.5)
                        self.assertEqual(response.status_code, 200)
                        self.assertFalse(changing.done(), "Model cleanup must still be waiting in its worker thread")
                    finally:
                        release.set()
                        await asyncio.wait_for(changing, 2)
                await self.client.post("/api/audio/stop")

    async def test_start_waits_for_warmup_configuration(self):
        entered = threading.Event()
        release = threading.Event()
        order = []

        def configure(settings, secrets):
            order.append("configure")
            if len(order) == 1:
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("Test model cleanup timed out")

        with patch.object(self.controller.audio, "configure", side_effect=configure), \
             patch.object(self.controller.audio, "warmup", side_effect=lambda: order.append("warmup")):
            warming = asyncio.create_task(self.client.post("/api/audio/warmup"))
            starting = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                starting = asyncio.create_task(self.client.post("/api/audio/start", json={"device_id": None}))
                await asyncio.sleep(0.05)
                self.assertEqual(order, ["configure"])
                self.assertFalse(starting.done())
            finally:
                release.set()
                pending = [warming] + ([starting] if starting is not None else [])
                responses = await asyncio.wait_for(asyncio.gather(*pending), 2)
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(order, ["configure", "warmup", "configure"])

    async def test_cancelling_before_generation_starts_marks_answer_cancelled(self):
        c = self.controller
        c.store.secrets["deepseek_key"] = "sk-review-unused"
        await c.start_answer("一个立即取消的问题", "answer")
        # Do not yield to the generated background task: cancel its pending
        # scheduling slot, the same race as back-to-back concurrent requests.
        await c.stop_answer()
        self.assertEqual(c.session["answers"][-1]["status"], "cancelled")

    async def test_answer_waits_for_profile_transition_and_excludes_old_history(self):
        c = self.controller
        first_profile = c.store.active_profile()
        next_profile = c.store.save_profile({"name": "第二家公司", "company": "公司 B"})
        c.store.secrets["deepseek_key"] = "sk-review-unused"
        c.session["answers"].append({"id": "previous-answer", "question": "公司 A 的旧问题", "text": "仅属于公司 A 的历史建议", "status": "done"})
        c.audio.stop_entered.clear()
        c.audio.stop_release.clear()
        recorded = []

        def build(profile, question, history, settings, mode):
            recorded.append({"profile": deepcopy(profile), "history": deepcopy(history)})
            return [{"role": "user", "content": question}], []

        async def stream(settings, secrets, messages):
            yield "第二家公司建议"

        with patch.object(intelligence, "build_messages", side_effect=build), patch.object(intelligence, "stream_answer", side_effect=stream):
            switching = asyncio.create_task(self.client.post("/api/profiles/active", json={"id": next_profile["id"]}))
            self.assertTrue(await asyncio.to_thread(c.audio.stop_entered.wait, 1))
            answering = asyncio.create_task(self.client.post("/api/answer", json={"question": "新公司的问题", "mode": "answer"}))
            await asyncio.sleep(0.05)
            c.audio.stop_release.set()
            switch_response, answer_response = await asyncio.wait_for(asyncio.gather(switching, answering), 2)
            if c.answer_task:
                await c.answer_task
        self.assertEqual(switch_response.status_code, 200)
        self.assertEqual(answer_response.status_code, 200)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["profile"]["id"], next_profile["id"])
        self.assertEqual(recorded[0]["history"], [], "New company context must never include the previous company's suggestions")
        self.assertNotEqual(first_profile["id"], c.session["profile_id"])
        self.assertEqual(c.session["answers"][0]["question"], "新公司的问题")


class LauncherReviewTests(unittest.TestCase):
    def test_launcher_configures_uvicorn_when_pythonw_has_no_standard_streams(self):
        import main as launcher

        class FakeServer:
            def __init__(self, config):
                self.config = config
                self.should_exit = False
                self.ran = False

            def run(self, sockets):
                self.ran = True

        servers = []

        def make_server(config):
            server = FakeServer(config)
            servers.append(server)
            return server

        with tempfile.TemporaryDirectory(prefix="interview-launch-review-") as directory:
            with patch.object(sys, "argv", ["main.py", "--no-window", "--port", "0", "--data-dir", directory]), \
                 patch.object(sys, "stdout", None), patch.object(sys, "stderr", None), \
                 patch.dict(os.environ), patch("logging.basicConfig"), \
                 patch("uvicorn.Server", side_effect=make_server):
                launcher.main()
            self.assertEqual(len(servers), 1)
            self.assertTrue(servers[0].ran)
            self.assertFalse((Path(directory) / "instance.json").exists())


if __name__ == "__main__":
    unittest.main()
