import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from interview_copilot import runtime
from interview_copilot.local_asr import LocalASRWorker


class FrozenRuntimeTests(unittest.TestCase):
    def test_source_data_dir_keeps_existing_user_profile_location(self):
        with patch.object(sys, "frozen", False, create=True), patch.dict(os.environ, {"LOCALAPPDATA": "elsewhere"}):
            self.assertEqual(runtime.default_data_dir(Path("existing-source")), Path("existing-source/data"))

    def test_frozen_data_stays_outside_bundle_and_source_data(self):
        with patch.object(sys, "frozen", True, create=True), patch.dict(os.environ, {"LOCALAPPDATA": "user-settings"}):
            self.assertEqual(runtime.default_data_dir(Path("read-only-bundle")), Path("user-settings/InterviewCopilot"))

    def test_frozen_data_has_user_directory_fallback(self):
        with patch.object(sys, "frozen", True, create=True), patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=Path("user")):
            self.assertEqual(runtime.default_data_dir(Path("bundle")), Path("user/AppData/Local/InterviewCopilot"))

    def test_frozen_children_use_console_companion_with_only_role_flag(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(Path("bundle/InterviewCopilot.exe"))):
            for flag in ("--asr-worker", "--overlay-window"):
                self.assertEqual(runtime.child_command(flag), [str(Path("bundle/InterviewCopilot-worker.exe")), flag])
            self.assertEqual(LocalASRWorker("tiny", Path("models"))._command, runtime.child_command("--asr-worker"))

    def test_source_children_use_python_script_without_frozen_flags(self):
        with patch.object(sys, "frozen", False, create=True):
            for flag, filename in (("--asr-worker", "asr_worker.py"), ("--overlay-window", "overlay_window.py")):
                command = runtime.child_command(flag)
                self.assertEqual(command[:2], [sys.executable, "-u"])
                self.assertEqual(Path(command[2]).name, filename)
                self.assertTrue(Path(command[2]).is_file())

    def test_dispatch_routes_private_flags_without_server_startup(self):
        with patch.object(runtime.runpy, "run_module") as run:
            self.assertTrue(runtime.dispatch_child(["--asr-worker"]))
            run.assert_called_once_with("interview_copilot.asr_worker", run_name="__main__")
            run.reset_mock()
            self.assertTrue(runtime.dispatch_child(["--overlay-window"]))
            run.assert_called_once_with("interview_copilot.overlay_window", run_name="__main__")
            run.reset_mock()
            self.assertFalse(runtime.dispatch_child(["--no-window"]))
            run.assert_not_called()

    def test_dispatch_rejects_extra_worker_arguments(self):
        with patch.object(runtime.runpy, "run_module") as run:
            with self.assertRaises(SystemExit):
                runtime.dispatch_child(["--asr-worker", "--port", "8765"])
            run.assert_not_called()

    def test_main_worker_flag_uses_binary_protocol_instead_of_starting_app(self):
        launcher = Path(runtime.__file__).resolve().parent.parent / "main.py"
        process = subprocess.run([sys.executable, str(launcher), "--asr-worker"],
            input=b'{"op":"unsupported"}\n', capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout.strip(), b'{"ok": false, "error": "protocol"}')
        self.assertEqual(process.stderr, b"")

    def test_windowed_missing_streams_are_replaced_but_pipes_preserved(self):
        pipe = io.StringIO()
        with patch.object(sys, "stdin", None), patch.object(sys, "stdout", pipe), patch.object(sys, "stderr", None):
            runtime.ensure_standard_streams()
            restored_input, restored_error = sys.stdin, sys.stderr
            try:
                self.assertIs(sys.stdout, pipe)
                self.assertEqual(sys.stdin.buffer.read(), b"")
                sys.stderr.write("safe logger output")
            finally:
                restored_input.close()
                restored_error.close()


@unittest.skipUnless(os.name == "nt", "Windows frozen browser environment")
class FrozenBrowserEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.bundle = str(Path("bundle/_internal").resolve())
        self.normal = str(Path("system-tools").resolve())
        self.sibling = str(Path("bundle/_internal-extra").resolve())
        self.before = os.pathsep.join((self.normal, self.bundle, str(Path(self.bundle) / "dlls"), self.sibling))
        self.after = os.pathsep.join((self.normal, self.sibling))
        self.api = Mock()
        def get_directory(size, output):
            if size:
                output.value = self.bundle
            return len(self.bundle) + (0 if size else 1)
        self.api.GetDllDirectoryW.side_effect = get_directory
        self.api.SetDllDirectoryW.return_value = 1
        for patcher in (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", self.bundle, create=True),
            patch.dict(os.environ, {"PATH": self.before}),
            patch.object(runtime.ctypes, "WinDLL", return_value=self.api),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_edge_inherits_system_paths_then_parent_restores_bundle(self):
        import main
        def launched(*args, **kwargs):
            self.assertEqual(kwargs["env"]["PATH"], self.after)
            self.assertEqual(os.environ["PATH"], self.after)
            self.assertEqual(kwargs["startupinfo"].wShowWindow, 1)
            self.api.SetDllDirectoryW.assert_called_once_with(None)
        with patch.object(Path, "is_file", return_value=True), patch("main.subprocess.Popen", side_effect=launched) as popen:
            main.open_window("http://127.0.0.1:8765", Path("data"))
            popen.assert_called_once()
        self.assertEqual(os.environ["PATH"], self.before)
        self.assertEqual([call.args for call in self.api.SetDllDirectoryW.call_args_list], [(None,), (self.bundle,)])

    def test_default_browser_fallback_is_clean_and_restored_after_failure(self):
        import main
        def launched(*args, **kwargs):
            self.assertEqual(os.environ["PATH"], self.after)
            self.api.SetDllDirectoryW.assert_called_once_with(None)
            raise OSError("browser failed")
        with patch.object(Path, "is_file", return_value=False), patch("main.webbrowser.open", side_effect=launched):
            with self.assertRaisesRegex(OSError, "browser failed"):
                main.open_window("http://127.0.0.1:8765", Path("data"))
        self.assertEqual(os.environ["PATH"], self.before)
        self.assertEqual([call.args for call in self.api.SetDllDirectoryW.call_args_list], [(None,), (self.bundle,)])

    def test_source_launch_does_not_change_dll_search_or_path(self):
        with patch.object(sys, "frozen", False):
            with runtime.external_browser_environment() as environment:
                self.assertIsNone(environment)
                self.assertEqual(os.environ["PATH"], self.before)
        self.api.SetDllDirectoryW.assert_not_called()


if __name__ == "__main__":
    unittest.main()
