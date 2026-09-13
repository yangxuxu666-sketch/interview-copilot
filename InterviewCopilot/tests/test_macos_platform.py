"""Platform contracts on any host; these do not claim native macOS verification."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import main
from interview_copilot import runtime, storage, overlay, overlay_window


class MemoryKeychain:
    def __init__(self):
        self.entries = {}

    def get_password(self, service, account):
        return self.entries.get((service, account))

    def set_password(self, service, account, value):
        self.entries[(service, account)] = value


class MacRuntimeTests(unittest.TestCase):
    def test_source_and_frozen_store_outside_application_bundle(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), patch.object(sys, "platform", "darwin"), \
                    patch.object(sys, "frozen", frozen, create=True), \
                    patch.object(Path, "home", return_value=Path("mac-user")):
                self.assertEqual(runtime.default_data_dir(Path("application.app/Contents/Resources")),
                                 Path("mac-user/Library/Application Support/InterviewCopilot"))

    def test_mac_frozen_children_are_sibling_macho_helpers(self):
        executable = Path("application.app/Contents/MacOS/InterviewCopilot")
        with patch.object(sys, "platform", "darwin"), patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(executable)):
            for flag in runtime.CHILD_MODULES:
                self.assertEqual(runtime.child_command(flag),
                                 [str(executable.with_name("InterviewCopilot-worker")), flag])

    def test_mac_browser_uses_launchservices_without_windows_browser_or_flags(self):
        with patch.object(sys, "platform", "darwin"), \
                patch.object(main, "external_browser_environment", return_value=nullcontext({"PATH": "/usr/bin"})), \
                patch.object(main.subprocess, "Popen") as launch, patch.object(main.webbrowser, "open") as fallback:
            main.open_window("http://127.0.0.1:8765/launch?token=test-only", Path("data"))
        self.assertEqual(launch.call_args.args[0], ["/usr/bin/open", "http://127.0.0.1:8765/launch?token=test-only"])
        self.assertEqual(launch.call_args.kwargs["env"], {"PATH": "/usr/bin"})
        self.assertNotIn("creationflags", launch.call_args.kwargs)
        self.assertNotIn("startupinfo", launch.call_args.kwargs)
        fallback.assert_not_called()

    def test_mac_external_environment_drops_bundle_libraries_without_changing_parent(self):
        env = {"PATH": os.pathsep.join((str(Path("system").resolve()), str(Path("bundle").resolve()))),
               "DYLD_LIBRARY_PATH": "bundle/libraries", "DYLD_FRAMEWORK_PATH": "bundle/frameworks",
               "DYLD_FRAMEWORK_PATH_ORIG": "system/frameworks", "DYLD_FALLBACK_LIBRARY_PATH": "bundle/fallback"}
        with patch.object(sys, "platform", "darwin"), patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "_MEIPASS", str(Path("bundle").resolve()), create=True), \
                patch.dict(os.environ, env, clear=True):
            with runtime.external_browser_environment() as child:
                self.assertNotIn("DYLD_LIBRARY_PATH", child)
                self.assertNotIn("DYLD_FALLBACK_LIBRARY_PATH", child)
                self.assertEqual(child["DYLD_FRAMEWORK_PATH"], "system/frameworks")
                self.assertEqual(child["PATH"], str(Path("system").resolve()))
                self.assertEqual(dict(os.environ), env)


class MacStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.keychain = MemoryKeychain()
        for item in (patch.object(sys, "platform", "darwin"),
                     patch.object(storage, "_mac_keychain", return_value=self.keychain)):
            item.start()
            self.addCleanup(item.stop)

    def test_key_round_trip_has_no_secret_on_disk(self):
        store = storage.Store(self.root)
        key = "synthetic-keychain-test-only"
        store.update_settings({"deepseek_key": key, "answer_style": "structured"})
        loaded = storage.Store(self.root)
        self.assertEqual(loaded.secrets["deepseek_key"], key)
        self.assertTrue(loaded.public_settings()["deepseek_key_set"])
        self.assertIsNone(loaded.secret_error)
        self.assertEqual(loaded.public_settings()["answer_style"], "structured")
        self.assertFalse((self.root / "secrets.dpapi").exists())
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(key.encode(), path.read_bytes())

    def test_custom_data_directory_cannot_read_another_installations_keys(self):
        storage.Store(self.root / "first").update_settings({"deepseek_key": "synthetic-test-only"})
        other = storage.Store(self.root / "second")
        self.assertFalse(other.public_settings()["deepseek_key_set"])
        self.assertEqual(other.secrets, {})

    def test_deleting_one_key_preserves_other_key_after_restart(self):
        store = storage.Store(self.root)
        store.update_settings({"deepseek_key": "synthetic-deepseek", "cloud_asr_key": "synthetic-asr"})
        store.update_settings({"delete_deepseek_key": True})
        loaded = storage.Store(self.root)
        self.assertEqual(loaded.secrets, {"cloud_asr_key": "synthetic-asr"})
        loaded.update_settings({"delete_cloud_asr_key": True})
        self.assertEqual(storage.Store(self.root).secrets, {})

    def test_locked_keychain_reports_failure_without_echoing_native_details(self):
        with patch.object(self.keychain, "get_password", side_effect=RuntimeError("secret-native-error")):
            store = storage.Store(self.root)
        self.assertIn("钥匙串", store.secret_error)
        self.assertNotIn("secret-native-error", store.secret_error)
        self.assertFalse(store.public_settings()["deepseek_key_set"])

    def test_failed_save_keeps_previous_key_and_settings(self):
        store = storage.Store(self.root)
        store.update_settings({"deepseek_key": "synthetic-first"})
        with patch.object(self.keychain, "set_password", side_effect=RuntimeError("secret-native-error")):
            with self.assertRaisesRegex(RuntimeError, "未能保存到 macOS 钥匙串") as error:
                store.update_settings({"deepseek_key": "synthetic-second", "answer_style": "structured"})
        self.assertNotIn("secret-native-error", str(error.exception))
        self.assertEqual(store.secrets["deepseek_key"], "synthetic-first")
        loaded = storage.Store(self.root)
        self.assertEqual(loaded.secrets["deepseek_key"], "synthetic-first")
        self.assertEqual(loaded.public_settings()["answer_style"], "concise")

    def test_saving_after_temporary_keychain_lock_preserves_unedited_key(self):
        storage.Store(self.root).update_settings({"deepseek_key": "synthetic-first", "cloud_asr_key": "synthetic-asr"})
        with patch.object(self.keychain, "get_password", side_effect=RuntimeError("Locked")):
            store = storage.Store(self.root)
            with self.assertRaisesRegex(RuntimeError, "原有 API 密钥尚未修改"):
                store.update_settings({"deepseek_key": "synthetic-second"})
        store.update_settings({"deepseek_key": "synthetic-second"})
        self.assertEqual(storage.Store(self.root).secrets,
                         {"deepseek_key": "synthetic-second", "cloud_asr_key": "synthetic-asr"})


class MacOverlayTests(unittest.TestCase):
    def test_mac_launch_keeps_control_pipe_and_does_not_use_windows_startup_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Mock()
            process.poll.return_value = None

            def input_written(raw):
                payload = json.loads(raw)
                Path(directory, "overlay-ready.json").write_text(json.dumps({
                    "mapped": True, "launch_id": payload["launch_id"]}), encoding="utf-8")

            process.stdin.write.side_effect = input_written
            with patch.object(sys, "platform", "darwin"), patch.object(overlay.subprocess, "Popen", return_value=process) as popen:
                owned = overlay.OverlayProcess()
                owned.open(8765, "synthetic-token", directory)
            self.assertTrue(owned.running)
            self.assertEqual(popen.call_args.kwargs["creationflags"], 0)
            self.assertIsNone(popen.call_args.kwargs["startupinfo"])
            self.assertEqual(popen.call_args.kwargs["stdin"], overlay.subprocess.PIPE)
            self.assertEqual(json.loads(process.stdin.write.call_args.args[0])["token"], "synthetic-token")

    def test_mac_overlay_never_calls_windows_capture_exclusion(self):
        window = overlay_window.AnswerWindow.__new__(overlay_window.AnswerWindow)
        window.capture_supported = False
        window.capture_excluded = Mock()
        window.capture_message = Mock()
        window.capture_label = Mock()
        window.write_ready = Mock()
        with patch.object(overlay_window, "set_capture_excluded") as affinity:
            window.apply_capture_privacy()
        affinity.assert_not_called()
        window.capture_excluded.set.assert_called_once_with(False)
        window.capture_message.set.assert_called_once_with("macOS 版不支持共享隐藏")
        self.assertIsNone(window.capture_affinity)
        window.write_ready.assert_called_once()


if __name__ == "__main__":
    unittest.main()
