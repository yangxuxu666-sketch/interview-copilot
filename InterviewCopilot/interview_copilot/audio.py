"""Output-only audio capture and endpoint-based speech recognition.

No device or network is opened on import. Raw audio lives only in bounded memory
queues. WASAPI loopback records the selected playback device, including every
application playing on it; it does not identify speakers or remove sidetone.
"""
from __future__ import annotations

from array import array
import asyncio
from collections import deque
from dataclasses import dataclass, field
from io import BytesIO
import importlib
import math
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import wave

from .local_asr import LocalASRError, LocalASRWorker


class AudioError(RuntimeError):
    """An actionable, user-facing audio error."""


class SpeechChunk(bytes):
    """PCM or an empty endpoint marker, both ordered through the same queue."""
    def __new__(cls, data, continuation=False, endpoint=False):
        obj = super().__new__(cls, data)
        obj.continuation = continuation
        obj.endpoint = endpoint
        return obj


class EnergySegmenter:
    """PCM16 energy endpoint detector with pre-roll and a strict duration bound.

    This is an energy detector, not speaker diarization. Local Whisper adds its
    own Silero speech filter. Counting voiced samples rejects brief clicks.
    """

    def __init__(self, sample_rate: int, channels: int, *, silence_ms=700,
                 min_speech_ms=300, max_segment_s=12, energy_threshold=0.008,
                 pre_roll_ms=240):
        if sample_rate <= 0 or channels <= 0:
            raise ValueError("采样率和声道数必须大于零")
        self.sample_rate = sample_rate
        self.channels = channels
        self.silence_samples = max(1, round(sample_rate * silence_ms / 1000))
        self.min_speech_samples = max(1, round(sample_rate * min_speech_ms / 1000))
        self.max_samples = max(1, round(sample_rate * max_segment_s))
        self.pre_roll_samples = min(round(sample_rate * pre_roll_ms / 1000), self.max_samples // 2)
        self.threshold = float(energy_threshold)
        self.level = 0.0
        self._pre: deque[tuple[bytes, int]] = deque()
        self._pre_samples = 0
        self._parts: list[bytes] = []
        self._total = self._voiced = self._silent = 0
        self._awaiting_endpoint = False
        self._endpoint_silence = 0

    def reset(self):
        self._pre.clear()
        self._pre_samples = 0
        self._parts.clear()
        self._total = self._voiced = self._silent = 0
        self._awaiting_endpoint = False
        self._endpoint_silence = 0

    def feed(self, pcm: bytes) -> list[bytes]:
        alignment = 2 * self.channels
        if len(pcm) % alignment:
            raise ValueError("PCM 音频帧不完整")
        count = len(pcm) // alignment
        if not count:
            self.level = 0.0
            return []
        # Handle unusually large callbacks without exceeding the segment bound.
        if count > self.max_samples:
            result = []
            step = self.max_samples * alignment
            for offset in range(0, len(pcm), step):
                result.extend(self.feed(pcm[offset:offset + step]))
            return result
        values = array("h")
        values.frombytes(pcm)
        if sys.byteorder != "little":
            values.byteswap()
        self.level = max(math.sqrt(sum(v * v for v in values[channel::self.channels]) / count)
                         for channel in range(self.channels)) / 32768
        # Hysteresis keeps a quieter syllable at the end of an already-started
        # phrase instead of treating it as silence and trimming it away.
        threshold = self.threshold * (0.6 if self._parts or self._awaiting_endpoint else 1.0)
        voiced = self.level >= threshold
        if not self._parts and not voiced:
            self._remember(pcm, count)
            if self._awaiting_endpoint:
                self._endpoint_silence += count
                if self._endpoint_silence >= self.silence_samples:
                    self._awaiting_endpoint = False
                    self._endpoint_silence = 0
                    # A speaker may finish exactly on the forced cut. An empty
                    # marker preserves FIFO ordering behind its ASR work without
                    # uploading/transcribing silence or waiting for another word.
                    return [SpeechChunk(b"", endpoint=True)]
            return []
        if not self._parts:
            self._endpoint_silence = 0
            self._parts = [part for part, _ in self._pre]
            self._total = self._pre_samples
            self._pre.clear()
            self._pre_samples = 0
        # A callback can cross a forced segment boundary. Split on a frame.
        room = self.max_samples - self._total
        if voiced and room <= 0:
            result = self._finish(forced=True)
            result.extend(self.feed(pcm))
            return result
        if voiced and count > room:
            first = pcm[:room * alignment]
            rest = pcm[room * alignment:]
            self._append(first, room, voiced)
            result = self._finish(forced=True)
            result.extend(self.feed(rest))
            return result
        self._append(pcm, count, voiced)
        if self._silent >= self.silence_samples or (voiced and self._total >= self.max_samples):
            return self._finish(forced=self._silent < self.silence_samples)
        return []

    def _remember(self, pcm, count):
        self._pre.append((pcm, count))
        self._pre_samples += count
        while self._pre_samples > self.pre_roll_samples and self._pre:
            excess = self._pre_samples - self.pre_roll_samples
            first, first_count = self._pre.popleft()
            removed = min(first_count, excess)
            self._pre_samples -= removed
            if removed < first_count:
                self._pre.appendleft((first[removed * 2 * self.channels:], first_count - removed))

    def _append(self, pcm, count, voiced):
        self._parts.append(pcm)
        self._total += count
        if voiced:
            self._voiced += count
            self._silent = 0
        else:
            self._silent += count

    def _finish(self, forced=False):
        was_awaiting = self._awaiting_endpoint
        # A brief remainder after a forced cut belongs to an existing utterance,
        # so do not apply the initial-click rejection threshold to that tail.
        accepted = self._voiced >= self.min_speech_samples or (was_awaiting and self._voiced > 0)
        data = b"".join(self._parts) if accepted else b""
        if accepted and self._silent:
            # Keep 150 ms of trailing silence, reducing ASR work.
            trim = max(0, self._silent - round(self.sample_rate * 0.15), self._total - self.max_samples)
            if trim:
                data = data[:-trim * 2 * self.channels]
        self.reset()
        self._awaiting_endpoint = forced and (accepted or was_awaiting)
        if data:
            return [SpeechChunk(data, continuation=forced)]
        if was_awaiting and not forced:
            return [SpeechChunk(b"", endpoint=True)]
        return []


def _wav_bytes(pcm: bytes, rate: int, channels: int) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _replace_oldest(target: queue.Queue, item) -> bool:
    """Never block the PortAudio callback; return whether an item was dropped."""
    try:
        target.put_nowait(item)
        return False
    except queue.Full:
        try:
            target.get_nowait()
        except queue.Empty:
            pass
        try:
            target.put_nowait(item)
        except queue.Full:
            pass
        return True


def _cloud_url(value: str) -> str:
    url = str(value).strip()
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.fragment
                 and parsed.path.rstrip("/").endswith("/audio/transcriptions"))
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise AudioError("请填写 HTTPS 云端语音接口完整地址，以 /audio/transcriptions 结尾；地址中不能包含账号密码。")
    return url


class PCM16StreamConverter:
    """Stateful audioop conversion to the realtime service's PCM16/16k/mono.

    A stable active channel avoids cancelling opposite-phase stereo. The
    CPython audioop backport preserves resampling state across input callbacks.
    """
    def __init__(self, rate, channels):
        if rate < 1 or channels < 1:
            raise AudioError("采样率和声道数必须大于零。")
        try:
            self._audioop = importlib.import_module("audioop")
        except ImportError as exc:
            raise AudioError("实时语音转换组件未就绪，请运行语音组件安装脚本。") from exc
        self.rate, self.channels = rate, channels
        self._state = None
        self._channel = None
        self.level = 0.0

    def feed(self, pcm):
        if len(pcm) % (2 * self.channels):
            raise AudioError("系统播放声音的 PCM 帧不完整。")
        if not pcm:
            return b""
        values = array("h", pcm)
        if sys.byteorder != "little":
            values.byteswap()
        count = len(values) // self.channels
        powers = [sum(v * v for v in values[channel::self.channels]) / count
                  for channel in range(self.channels)]
        strongest = max(range(self.channels), key=lambda channel: powers[channel])
        if self._channel is None or powers[self._channel] < powers[strongest] * 0.1:
            self._channel = strongest
        self.level = math.sqrt(max(powers)) / 32768
        mono = values[self._channel::self.channels]
        if sys.byteorder != "little":
            mono.byteswap()
        converted, self._state = self._audioop.ratecv(mono.tobytes(), 2, 1,
                                                     self.rate, 16000, self._state)
        return converted


@dataclass
class _Run:
    settings: dict
    secrets: dict
    device: dict
    stop: threading.Event = field(default_factory=threading.Event)
    frames: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=40))
    segments: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=2))
    capture_thread: threading.Thread | None = None
    asr_thread: threading.Thread | None = None
    dropped_frames: int = 0
    dropped_segments: int = 0
    cloud_loop: object = None
    cloud_task: object = None
    qwen_client: object = None
    qwen_ready: threading.Event = field(default_factory=threading.Event)
    qwen_audio: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=12))


class AudioService:
    def __init__(self, data_dir: Path, emit: Callable[[dict], None],
                 on_transcript: Callable[[dict], None]):
        self.data_dir = Path(data_dir)
        self.emit = emit
        self.on_transcript = on_transcript
        self._lock = threading.RLock()
        self._settings: dict = {}
        self._secrets: dict = {}
        self._run: _Run | None = None
        self._model = None
        self._model_name: str | None = None
        self._warmup_thread: threading.Thread | None = None
        self._warmup_cancel = threading.Event()
        self._asr_generation = 0
        self._closed = False
        self._state = {"state": "idle", "message": "尚未监听", "listening": False,
                       "asr_state": "idle", "asr_message": "语音识别待命"}

    def configure(self, settings: dict, secrets: dict):
        obsolete = None
        with self._lock:
            if self._run:
                raise AudioError("请先停止监听，再修改语音设置。")
            previous = (self._settings.get("asr_provider", "local"), self._settings.get("whisper_model", "base"))
            selected = (settings.get("asr_provider", "local"), settings.get("whisper_model", "base"))
            if previous != selected:
                self._asr_generation += 1
                self._warmup_cancel.set()
                self._warmup_thread = None
                obsolete, self._model = self._model, None
                self._model_name = None
                self._status("asr", "idle", "语音设置已更新，模型需要重新加载。")
            self._settings = dict(settings)
            self._secrets = dict(secrets)
        if obsolete is not None:
            obsolete.close()

    @staticmethod
    def _pyaudio():
        if sys.platform == "darwin":
            from . import macos_audio
            return macos_audio
        if sys.platform != "win32":
            raise AudioError("系统声音监听支持 Windows 和 macOS 13 及以上。手动输入问题仍可使用。")
        try:
            return importlib.import_module("pyaudiowpatch")
        except (ImportError, OSError) as exc:
            raise AudioError("系统声音组件未就绪，请运行安装脚本安装 requirements-audio.txt（PyAudioWPatch）。") from exc

    def devices(self) -> list[dict]:
        module = self._pyaudio()
        try:
            with module.PyAudio() as manager:
                host = manager.get_host_api_info_by_type(module.paWASAPI)
                try:
                    default_id = int(manager.get_default_wasapi_loopback()["index"])
                except (OSError, ValueError):
                    default_id = None
                return [{"id": int(device["index"]), "name": str(device["name"]),
                         "is_default": int(device["index"]) == default_id,
                         "sample_rate": int(device["defaultSampleRate"]),
                         "channels": int(device["maxInputChannels"])}
                        for device in manager.get_loopback_device_info_generator()
                        if device.get("isLoopbackDevice")
                        and device.get("hostApi") == host["index"]
                        and device.get("maxInputChannels", 0) > 0]
        except (OSError, ValueError) as exc:
            raise AudioError("无法枚举系统播放设备。请连接耳机或扬声器，并检查系统声音设置。") from exc

    def start(self, device_id=None):
        with self._lock:
            if self._closed:
                raise AudioError("语音服务已关闭，请重新启动工具。")
            if self._run:
                return
            settings, secrets = dict(self._settings), dict(self._secrets)
            provider = settings.get("asr_provider", "local")
            if provider == "qwen":
                if not secrets.get("cloud_asr_key"):
                    raise AudioError("请先填写阿里云百炼语音 API Key。DeepSeek Key 不用于语音识别。")
            elif provider == "cloud":
                _cloud_url(settings.get("cloud_asr_url", ""))
                if not secrets.get("cloud_asr_key"):
                    raise AudioError("请先填写云端语音识别 API Key。DeepSeek Key 不用于语音识别。")
                if not str(settings.get("cloud_asr_model", "")).strip():
                    raise AudioError("请填写云端语音识别模型名称。")
            elif provider != "local":
                raise AudioError("请选择阿里云实时识别、本地识别或兼容云端识别。")
            module = self._pyaudio()
            try:
                # PortAudio/WASAPI initialization carries thread-local COM
                # state. Finish this temporary enumeration before the capture
                # thread creates and owns its own manager and stream.
                with module.PyAudio() as manager:
                    host = manager.get_host_api_info_by_type(module.paWASAPI)
                    selected = settings.get("device_id") if device_id is None else device_id
                    device = (manager.get_default_wasapi_loopback() if selected is None
                              else manager.get_device_info_by_index(int(selected)))
                    # Fail closed: never fall back to a microphone, even if an
                    # API client supplies a physical input device identifier.
                    if not device.get("isLoopbackDevice") or device.get("hostApi") != host["index"]:
                        raise AudioError("只能选择系统播放声音设备，不能选择麦克风。")
                    if device.get("maxInputChannels", 0) < 1:
                        raise AudioError("所选播放设备不支持声音回环采集。")
                run = _Run(settings, secrets, dict(device))
                self._run = run
                self._status("audio", "loading", "正在打开系统播放设备…", run)
                run.capture_thread = threading.Thread(target=self._capture, args=(run, module),
                                                      name="system-playback", daemon=True)
                run.asr_thread = threading.Thread(target=self._recognize, args=(run,),
                                                  name="interview-asr", daemon=True)
                run.capture_thread.start()
                run.asr_thread.start()
            except BaseException as exc:
                if self._run:
                    self._run.stop.set()
                    self._run = None
                self._state.update(state="error", listening=False)
                if isinstance(exc, AudioError):
                    raise
                raise AudioError("无法打开系统播放设备。请重新选择当前耳机/扬声器；蓝牙模式切换后需刷新设备。") from exc

    def _active(self, run):
        return self._run is run and not run.stop.is_set() and not self._closed

    def _emit_run(self, run, event):
        with self._lock:
            if self._active(run):
                self.emit(event)

    def _status(self, component, state, message, run=None):
        with self._lock:
            if self._closed or (run is not None and not self._active(run)):
                return
            if component == "audio":
                self._state.update(state=state, message=message,
                                   listening=state in {"loading", "listening"})
            else:
                self._state.update(asr_state=state, asr_message=message)
            self.emit({"type": "status", "component": component, "state": state, "message": message})

    def _fail(self, run, message):
        worker = None
        qwen_client = None
        with self._lock:
            if not self._active(run):
                return
            run.stop.set()
            self._run = None
            qwen_client = run.qwen_client
            for pending in (run.frames, run.segments, run.qwen_audio):
                while True:
                    try:
                        pending.get_nowait()
                    except queue.Empty:
                        break
            if run.settings.get("asr_provider", "local") == "local":
                self._asr_generation += 1
                self._warmup_cancel.set()
                self._warmup_thread = None
                worker, self._model = self._model, None
                self._model_name = None
            self._status("audio", "error", message)
            self._status("asr", "error", message)
            self.emit({"type": "error", "message": message})
        if worker is not None:
            worker.close()
        if qwen_client is not None:
            qwen_client.close()

    def _capture(self, run, module):
        manager = stream = None
        try:
            realtime = run.settings.get("asr_provider") == "qwen"
            # Begin capture only once the realtime connection is ready, so TLS
            # setup cannot leave the user several seconds behind the speaker.
            if realtime:
                while not run.qwen_ready.wait(0.1):
                    if run.stop.is_set():
                        return
            if run.stop.is_set():
                return
            manager = module.PyAudio()
            host = manager.get_host_api_info_by_type(module.paWASAPI)
            device = manager.get_device_info_by_index(int(run.device["index"]))
            # The device list can change between enumeration and opening. Never
            # trust an old index if it now points at a microphone/other output.
            if (not device.get("isLoopbackDevice") or device.get("hostApi") != host["index"]
                    or device.get("maxInputChannels", 0) < 1
                    or device.get("name") != run.device.get("name")):
                raise AudioError("播放设备在启动时发生变化。请刷新设备并重新选择当前耳机或扬声器。")
            with self._lock:
                if not self._active(run):
                    return
                run.device = dict(device)
            rate, channels = int(device["defaultSampleRate"]), int(device["maxInputChannels"])
            detector = None if realtime else EnergySegmenter(rate, channels, **{key: run.settings[key] for key in
                ("silence_ms", "min_speech_ms", "max_segment_s", "energy_threshold") if key in run.settings})
            converter = PCM16StreamConverter(rate, channels) if realtime else None
            realtime_pcm = bytearray()
            def callback(data, frame_count, time_info, flags):
                if run.stop.is_set():
                    return (None, module.paComplete)
                if data and _replace_oldest(run.frames, data):
                    run.dropped_frames += 1
                return (None, module.paContinue)
            if run.stop.is_set():
                return
            stream = manager.open(format=module.paInt16, channels=channels, rate=rate,
                                  input=True, input_device_index=int(device["index"]),
                                  frames_per_buffer=max(256, round(rate * 0.03)), stream_callback=callback,
                                  **({"cancel_event": run.stop} if sys.platform == "darwin" else {}))
            self._status("audio", "listening", "正在监听：" + str(device["name"]), run)
            last_level = last_warn = 0.0
            previous_drops = 0
            while not run.stop.is_set():
                try:
                    pcm = run.frames.get(timeout=0.15)
                except queue.Empty:
                    if run.stop.is_set():
                        break
                    if not stream.is_active():
                        raise AudioError("系统声音流已中断，请刷新并重新选择播放设备。")
                    # Some Windows drivers stop sending packets when all apps
                    # become silent. Advance endpoint timing using silence so
                    # the final question is still recognized after playback ends.
                    pcm = b"\0\0" * round(rate * 0.15) * channels
                if run.stop.is_set():
                    break
                if run.dropped_frames != previous_drops:
                    # A discontinuity must not splice unrelated words together.
                    if realtime:
                        raise AudioError("系统声音采集出现积压，实时识别已停止。请关闭占用资源的程序后重新监听。")
                    detector.reset()
                    previous_drops = run.dropped_frames
                if realtime:
                    realtime_pcm.extend(converter.feed(pcm))
                    while len(realtime_pcm) >= 3200 and not run.stop.is_set():
                        packet = bytes(realtime_pcm[:3200])
                        del realtime_pcm[:3200]
                        try:
                            run.qwen_audio.put_nowait(packet)
                        except queue.Full as exc:
                            raise AudioError("云端语音连接发送积压，已停止监听以避免延迟累积。请检查网络后重试。") from exc
                    level = converter.level
                else:
                    for segment in detector.feed(pcm):
                        if _replace_oldest(run.segments, segment):
                            run.dropped_segments += 1
                    level = detector.level
                now = time.monotonic()
                if now - last_level >= 0.10:
                    self._emit_run(run, {"type": "level", "value": min(1.0, level * 5)})
                    last_level = now
                if (run.dropped_frames or run.dropped_segments) and now - last_warn > 15:
                    self._emit_run(run, {"type": "warning", "message":
                        "语音识别暂时跟不上播放速度，已跳过最旧片段以降低延迟。可改用 tiny 模型或更快的云端识别。"})
                    last_warn = now
        except Exception as exc:
            self._fail(run, str(exc) if isinstance(exc, AudioError) else
                       "系统声音采集失败。请确认设备仍已连接，关闭占用音频的独占模式，再重试。")
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if manager is not None:
                manager.terminate()

    def _get_model(self, name, *, run=None, generation=None, cancel=None):
        if name not in {"tiny", "base", "small"}:
            raise AudioError("本地语音模型请选择 tiny、base 或 small。")
        obsolete = None
        with self._lock:
            if (self._closed or (run is not None and not self._active(run))
                    or (generation is not None and generation != self._asr_generation)
                    or (cancel is not None and cancel.is_set())):
                raise AudioError("本地语音识别已取消。")
            if self._model is None or self._model_name != name:
                obsolete = self._model
                self._model = LocalASRWorker(name, self.data_dir / "models")
                self._model_name = name
            model = self._model
        if obsolete is not None:
            obsolete.close()
        try:
            model.initialize(cancel)
        except LocalASRError as exc:
            with self._lock:
                if self._model is model:
                    self._model = self._model_name = None
            model.close()
            raise AudioError(str(exc)) from exc
        return model

    def warmup(self):
        with self._lock:
            if self._closed:
                raise AudioError("语音服务已关闭。")
            if self._settings.get("asr_provider", "local") != "local":
                self._status("asr", "ready", "云端识别无需本地模型预热。")
                return
            if self._warmup_thread and self._warmup_thread.is_alive():
                return
            name = self._settings.get("whisper_model", "base")
            generation = self._asr_generation
            cancel = self._warmup_cancel = threading.Event()
            self._status("asr", "loading", "正在预热本地模型；首次使用会下载模型到 data/models…")
            def load():
                try:
                    model = self._get_model(name, generation=generation, cancel=cancel)
                    with self._lock:
                        if (not cancel.is_set() and generation == self._asr_generation
                                and self._model is model and model.ready):
                            self._status("asr", "ready", "本地语音模型已就绪（CPU int8）。")
                except Exception as exc:
                    with self._lock:
                        if not cancel.is_set() and generation == self._asr_generation:
                            self._status("asr", "error", str(exc) if isinstance(exc, AudioError) else "本地模型预热失败，请重试。")
            self._warmup_thread = threading.Thread(target=load, name="asr-warmup", daemon=True)
            self._warmup_thread.start()

    def _recognize(self, run):
        if run.settings.get("asr_provider") == "qwen":
            self._recognize_qwen(run)
            return
        try:
            local = run.settings.get("asr_provider", "local") == "local"
            model = None
            if local:
                self._status("asr", "loading", "正在加载本地语音模型；首次可能需要下载…", run)
                model = self._get_model(run.settings.get("whisper_model", "base"), run=run, cancel=run.stop)
            self._status("asr", "ready", "语音识别已就绪", run)
            while not run.stop.is_set():
                try:
                    pcm = run.segments.get(timeout=0.15)
                except queue.Empty:
                    continue
                if run.stop.is_set():
                    break
                if getattr(pcm, "endpoint", False):
                    # Do not test bool(pcm): the empty bytes marker is deliberate.
                    # Its position in the queue ensures preceding ASR finishes
                    # before the application closes a continuation group.
                    with self._lock:
                        if self._active(run):
                            self.on_transcript({"text": "", "asr_ms": 0,
                                                "continuation": False, "endpoint": True})
                    continue
                self._status("asr", "transcribing", "正在识别刚刚的提问…", run)
                started = time.monotonic()
                wav = _wav_bytes(pcm, int(run.device["defaultSampleRate"]), int(run.device["maxInputChannels"]))
                if local:
                    text = self._transcribe_local(model, wav, run)
                else:
                    text = self._transcribe_cloud(wav, run)
                elapsed = round((time.monotonic() - started) * 1000)
                # Hold the state lock through delivery: stop() cannot return and
                # then receive a stale transcript from this generation.
                with self._lock:
                    continuation = bool(getattr(pcm, "continuation", False))
                    if self._active(run):
                        if text.strip():
                            self.on_transcript({"text": text.strip(), "asr_ms": elapsed,
                                                "continuation": continuation})
                        elif not continuation:
                            # VAD/ASR may reject a very short final voiced tail;
                            # the acoustic endpoint still ends prior text.
                            self.on_transcript({"text": "", "asr_ms": elapsed,
                                                "continuation": False, "endpoint": True})
                self._status("asr", "ready", "语音识别已就绪", run)
        except Exception as exc:
            self._fail(run, str(exc) if isinstance(exc, AudioError) else
                       "语音识别失败，监听已停止。请检查模型、语音接口和网络后重试。")

    def _recognize_qwen(self, run):
        try:
            from .qwen_asr import QwenASRError, QwenASRStream
        except ImportError:
            self._fail(run, "阿里云实时语音组件未就绪，请运行语音组件安装脚本。")
            return
        client = None
        try:
            def final(text):
                if not isinstance(text, str) or not text.strip():
                    return
                with self._lock:
                    if self._active(run):
                        # Only the service's final event becomes an interview
                        # question. Partial text never starts answer generation.
                        self.on_transcript({"text": text.strip(), "continuation": False,
                                            "asr_ms": None, "provider": "qwen"})
                        self.emit({"type": "transcript_partial", "text": ""})
                        self._status("asr", "ready", "阿里云实时识别正在倾听…", run)
            def partial(text):
                if isinstance(text, str):
                    self._emit_run(run, {"type": "transcript_partial", "text": text})
            client = QwenASRStream(run.secrets.get("cloud_asr_key", ""),
                language=run.settings.get("asr_language", "auto"),
                silence_ms=run.settings.get("silence_ms", 700), on_final=final, on_partial=partial)
            with self._lock:
                if not self._active(run):
                    return
                run.qwen_client = client
            self._status("asr", "loading", "正在连接阿里云实时语音识别…", run)
            client.connect(cancel=run.stop)
            if run.stop.is_set():
                return
            run.qwen_ready.set()
            self._status("asr", "ready", "阿里云实时语音识别已连接。", run)
            while not run.stop.is_set():
                try:
                    packet = run.qwen_audio.get(timeout=0.1)
                except queue.Empty:
                    client.check_error()
                    continue
                if run.stop.is_set():
                    break
                client.send_audio(packet)
                client.check_error()
        except (AudioError, QwenASRError) as exc:
            self._fail(run, str(exc))
        except Exception:
            self._fail(run, "阿里云实时语音连接中断，请检查网络及百炼 API Key 后重新监听。")
        finally:
            # Close outside AudioService's state lock. SDK callbacks may enter
            # that lock, so closing under it could deadlock stop vs callback.
            if client is not None:
                client.close()

    def _transcribe_local(self, model, wav, run):
        if run.stop.is_set():
            return ""
        try:
            language = run.settings.get("asr_language", "auto")
            text = model.transcribe(wav, language=None if language == "auto" else language,
                quality=run.settings.get("asr_quality", "balanced"),
                cancel=run.stop)
            return "" if run.stop.is_set() else text
        except LocalASRError as exc:
            if run.stop.is_set():
                return ""
            raise AudioError(str(exc)) from exc

    @staticmethod
    def _transcribe_cloud(wav, run):
        import httpx
        url = _cloud_url(run.settings.get("cloud_asr_url", ""))
        payload = {"model": str(run.settings["cloud_asr_model"])}
        endpoint = urlsplit(url)
        sensevoice_endpoint = (endpoint.hostname in {"api.siliconflow.cn", "api.siliconflow.com"}
                              and endpoint.path == "/v1/audio/transcriptions"
                              and payload["model"] == "FunAudioLLM/SenseVoiceSmall")
        # This provider documents only file/model and detects the language itself.
        # Keep the full OpenAI-compatible fields for every other endpoint/model.
        if not sensevoice_endpoint:
            payload["response_format"] = "json"
            language = run.settings.get("asr_language", "auto")
            if language != "auto":
                payload["language"] = language
        if run.stop.is_set():
            return ""
        async def request():
            run.cloud_loop = asyncio.get_running_loop()
            run.cloud_task = asyncio.current_task()
            if run.stop.is_set():
                return None
            # No redirect: a custom server cannot forward the Authorization
            # header or recorded audio to another host through a redirect.
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=8), follow_redirects=False) as client:
                    return await client.post(url, headers={"Authorization": "Bearer " + run.secrets["cloud_asr_key"]},
                        data=payload, files={"file": ("segment.wav", wav, "audio/wav")})
        try:
            response = asyncio.run(request())
            if response is None or run.stop.is_set():
                return ""
            if response.status_code in {401, 403}:
                raise AudioError("云端语音识别鉴权失败，请检查语音服务专用 API Key。")
            if response.status_code == 429:
                raise AudioError("云端语音识别请求受限或余额不足，请检查服务额度。")
            if not response.is_success:
                raise AudioError(f"云端语音识别返回 HTTP {response.status_code}；请核对接口地址与模型名称。")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                raise AudioError("云端语音接口未返回 JSON text 字段，请使用兼容的 /audio/transcriptions 接口。")
            return data["text"]
        except asyncio.CancelledError:
            return ""
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise AudioError("云端语音识别超时，请检查网络或改用本地识别。") from exc
        except httpx.RequestError as exc:
            raise AudioError("无法连接云端语音服务，请检查 HTTPS 地址和网络。") from exc
        except ValueError as exc:
            raise AudioError("云端语音接口未返回有效 JSON，请核对接口类型。") from exc
        finally:
            run.cloud_loop = run.cloud_task = None

    def stop(self):
        qwen_client = None
        with self._lock:
            self._asr_generation += 1
            self._warmup_cancel.set()
            self._warmup_thread = None
            worker, self._model = self._model, None
            self._model_name = None
            run = self._run
            self._run = None
            if run:
                run.stop.set()
                qwen_client = run.qwen_client
                if run.cloud_loop and run.cloud_task:
                    try:
                        run.cloud_loop.call_soon_threadsafe(run.cloud_task.cancel)
                    except RuntimeError:
                        pass  # The HTTP worker already closed its event loop.
                # Discard buffered audio and pending segments on explicit stop.
                for pending in (run.frames, run.segments, run.qwen_audio):
                    while True:
                        try:
                            pending.get_nowait()
                        except queue.Empty:
                            break
            self._status("audio", "idle", "监听已停止")
            self._status("asr", "idle", "语音识别已停止")
            if not self._closed:
                self.emit({"type": "level", "value": 0})
                self.emit({"type": "transcript_partial", "text": ""})
        if worker is not None:
            worker.close()
        if qwen_client is not None:
            qwen_client.close()
        if run and run.capture_thread and run.capture_thread is not threading.current_thread():
            run.capture_thread.join(timeout=0.75)
        if run and run.asr_thread and run.asr_thread is not threading.current_thread():
            run.asr_thread.join(timeout=0.75)

    def status(self) -> dict:
        with self._lock:
            run = self._run
            name = self._settings.get("whisper_model", "base")
            realtime = self._settings.get("asr_provider") == "qwen"
            return {**self._state, "listening": run is not None and not run.stop.is_set(),
                    "provider": self._settings.get("asr_provider", "local"),
                    "model": "qwen3-asr-flash-realtime" if realtime else name,
                    "model_ready": self._model is not None and self._model_name == name and self._model.ready,
                    "device_id": int(run.device["index"]) if run else None,
                    "device_name": str(run.device["name"]) if run else None,
                    "queue_depth": (run.qwen_audio.qsize() if realtime else run.segments.qsize()) if run else 0,
                    "dropped_segments": run.dropped_segments if run else 0,
                    "dropped_frames": run.dropped_frames if run else 0}

    def close(self):
        with self._lock:
            self._closed = True
        self.stop()
