"""Real IPC/lifecycle tests with stdlib-only child processes; no model/hardware."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from interview_copilot.local_asr import LocalASRCancelled, LocalASRError, LocalASRWorker


STUB = r'''
import base64, json, os, sys, time
mode = sys.argv[1]
count = 0
for line in sys.stdin.buffer:
    request = json.loads(line)
    op = request["op"]
    if mode == "crash_" + op:
        os._exit(37)
    if mode == "hang_" + op:
        time.sleep(30)
    if mode == "bad_" + op:
        print("invalid json", flush=True)
        continue
    if op == "init":
        count += 1
        response = {"ok": True}
    else:
        audio = base64.b64decode(request["wav"])
        response = {"ok": True, "text": json.dumps({"count": count, "pid": os.getpid(),
            "audio": list(audio), "language": request["language"], "text": "中文 English"})}
    print(json.dumps(response), flush=True)
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.close()
        self.temp.cleanup()

    def make_worker(self, mode="normal", **kwargs):
        worker = LocalASRWorker("tiny", Path(self.temp.name) / "models",
            _command=[sys.executable, "-u", "-c", STUB, mode], **kwargs)
        self.workers.append(worker)
        return worker

    def test_construction_is_lazy(self):
        worker = self.make_worker()
        self.assertIsNone(worker._process)
        self.assertFalse(worker.ready)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_process_and_model_reused_with_exact_memory_audio(self):
        worker = self.make_worker()
        worker.initialize()
        self.assertTrue(worker.ready)
        one = json.loads(worker.transcribe(b"\0\1\xffwav", language=None))
        two = json.loads(worker.transcribe(b"\0\2", language="en"))
        self.assertEqual(one["pid"], two["pid"])
        self.assertEqual(one["count"], 1)
        self.assertEqual(two["count"], 1)
        self.assertEqual(bytes(one["audio"]), b"\0\1\xffwav")
        self.assertEqual(one["text"], "中文 English")
        self.assertIsNone(one["language"])
        self.assertEqual(two["language"], "en")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_child_exit_while_initializing_is_reported_with_exit_code(self):
        worker = self.make_worker("crash_init")
        with self.assertRaisesRegex(LocalASRError, "0x00000025"):
            worker.initialize()
        self.assertFalse(worker.ready)
        self.assertIsNotNone(worker._process.poll())

    def test_child_exit_while_transcribing_is_reported(self):
        worker = self.make_worker("crash_transcribe")
        worker.initialize()
        with self.assertRaisesRegex(LocalASRError, "子进程已退出"):
            worker.transcribe(b"wav")
        self.assertFalse(worker.ready)
        self.assertIsNotNone(worker._process.poll())

    def test_initialization_timeout_terminates_child(self):
        worker = self.make_worker("hang_init", startup_timeout=0.4)
        started = time.monotonic()
        with self.assertRaisesRegex(LocalASRError, "加载/预热超时"):
            worker.initialize()
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(worker.ready)
        self.assertIsNotNone(worker._process.poll())

    def test_inference_timeout_terminates_child(self):
        worker = self.make_worker("hang_transcribe", inference_timeout=0.2)
        worker.initialize()
        with self.assertRaisesRegex(LocalASRError, "识别超时"):
            worker.transcribe(b"wav")
        self.assertFalse(worker.ready)
        self.assertIsNotNone(worker._process.poll())

    def test_close_interrupts_inference_and_permanently_rejects_old_worker(self):
        worker = self.make_worker("hang_transcribe")
        worker.initialize()
        results = []
        def transcribe():
            try:
                results.append(worker.transcribe(b"wav"))
            except LocalASRCancelled:
                results.append("cancelled")
        thread = threading.Thread(target=transcribe)
        thread.start()
        worker.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["cancelled"])
        self.assertIsNotNone(worker._process.poll())
        with self.assertRaises(LocalASRCancelled):
            worker.transcribe(b"new wav")

    def test_cancel_event_interrupts_loading_and_terminates_child(self):
        worker = self.make_worker("hang_init")
        cancel = threading.Event()
        results = []
        def initialize():
            try:
                worker.initialize(cancel)
            except LocalASRCancelled:
                results.append("cancelled")
        thread = threading.Thread(target=initialize)
        thread.start()
        deadline = time.monotonic() + 2
        while worker._process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(worker._process)
        cancel.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["cancelled"])
        self.assertIsNotNone(worker._process.poll())

    def test_invalid_child_response_reports_protocol_error(self):
        worker = self.make_worker("bad_init")
        with self.assertRaisesRegex(LocalASRError, "通信失败"):
            worker.initialize()
        self.assertIsNotNone(worker._process.poll())


if __name__ == "__main__":
    unittest.main()
