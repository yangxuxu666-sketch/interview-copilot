"""Audio-only ScreenCaptureKit adapter for macOS 13 and later.

This deliberately implements only the PyAudio surface consumed by audio.py.
The legacy ``paWASAPI`` names below are compatibility identifiers, not a driver
on macOS. Device enumeration is synthetic and does not request permission.
Only open() requests shareable content and registers an AUDIO stream output;
no microphone, video output, recording file, or virtual audio driver is used.

References: Apple's SCStreamConfiguration/sampleRate and channelCount docs;
PyObjC's ScreenCaptureKit, CoreMedia and dispatch framework API notes. Native
capture requires validation on a real Mac; portable tests validate conversion,
permission/start cancellation, output selection and cleanup with fake bindings.
"""
from __future__ import annotations

from array import array
import importlib
import math
import platform
import sys
import threading
import time
from types import SimpleNamespace

from .audio import AudioError


RATE = 16000
CHANNELS = 1
DEVICE_NAME = "Mac 系统播放声音（不采集麦克风）"
PERMISSION_HELP = ("请在“系统设置 → 隐私与安全性 → 屏幕与系统音频录制”中允许面试伴航，"
                   "然后完全退出并重新打开工具。macOS 13 中此权限名为“屏幕录制”。")
paWASAPI = 1013  # Internal compatibility identifier; never passed to PortAudio.
paInt16 = 8
paContinue = 0
paComplete = 1
_LPCM = int.from_bytes(b"lpcm", "big")
_FLOAT = 1
_BIG_ENDIAN = 2
_SIGNED_INT = 4
_PACKED = 8
_NON_INTERLEAVED = 32
_handler_type = None
_handler_lock = threading.Lock()


class MacOSAudioError(AudioError):
    """Actionable ScreenCaptureKit error, handled by the shared audio service."""


def _load_frameworks():
    if sys.platform != "darwin":
        raise MacOSAudioError("Mac 系统声音监听仅适用于 macOS 13 或更高版本。")
    try:
        version = tuple(int(part) for part in platform.mac_ver()[0].split(".")[:2])
    except ValueError:
        version = ()
    if version < (13,):
        raise MacOSAudioError("系统声音监听需要 macOS 13 或更高版本；当前仍可手动输入问题。")
    try:
        return SimpleNamespace(
            objc=importlib.import_module("objc"),
            Foundation=importlib.import_module("Foundation"),
            # Register AudioStreamBasicDescription's named fields before a
            # CoreMedia return value is decoded by the Objective-C bridge.
            CoreAudio=importlib.import_module("CoreAudio"),
            CM=importlib.import_module("CoreMedia"),
            SCK=importlib.import_module("ScreenCaptureKit"),
            dispatch=importlib.import_module("dispatch"),
        )
    except (ImportError, OSError) as exc:
        raise MacOSAudioError("Mac 系统音频组件未就绪。请重新安装 Mac 版或安装 requirements-macos.txt。") from exc


def _device():
    return {"index": 0, "name": DEVICE_NAME, "isLoopbackDevice": True,
            "hostApi": paWASAPI, "defaultSampleRate": RATE,
            "maxInputChannels": CHANNELS}


def _error_text(error, action):
    try:
        code = int(error.code())
    except (AttributeError, TypeError, ValueError):
        code = None
    if code == -3801:  # SCStreamErrorUserDeclined
        return "尚未获得 Mac 系统声音录制权限。" + PERMISSION_HELP
    suffix = "" if code is None else f"（系统错误 {code}）"
    return f"{action}{suffix}。请检查系统录制权限及显示器连接后重试。" + PERMISSION_HELP


def _pcm16_from_payload(payload, description, frame_count):
    """Validate the actual ASBD before interpreting a mono linear PCM buffer.

    ScreenCaptureKit normally emits float32. Requiring mono makes planar and
    interleaved storage identical, and CMBlockBufferCopyDataBytes handles any
    discontiguous underlying memory without borrowing pointers past a callback.
    """
    if not 0 <= frame_count <= RATE:
        raise MacOSAudioError("Mac 系统音频回调长度异常，已停止监听。")
    if (int(description.mFormatID) != _LPCM
            or float(description.mSampleRate) != RATE
            or int(description.mChannelsPerFrame) != CHANNELS):
        raise MacOSAudioError("Mac 返回了不匹配的系统音频格式，请重新开始监听。")
    flags = int(description.mFormatFlags)
    bits = int(description.mBitsPerChannel)
    if flags & ~(_FLOAT | _BIG_ENDIAN | _SIGNED_INT | _PACKED | _NON_INTERLEAVED):
        raise MacOSAudioError("Mac 返回了不支持的音频排列格式，已停止监听。")
    floating = bool(flags & _FLOAT)
    if (floating and (bits not in (32, 64) or flags & _SIGNED_INT)
            or not floating and (bits != 16 or not flags & _SIGNED_INT)):
        raise MacOSAudioError("Mac 返回了不支持的 PCM 采样格式，已停止监听。")
    width = bits // 8
    if int(description.mBytesPerFrame) != width or len(payload) != frame_count * width:
        raise MacOSAudioError("Mac 系统音频帧不完整，已停止监听以避免识别错乱。")
    if not frame_count:
        return b""
    values = array({16: "h", 32: "f", 64: "d"}[bits])
    values.frombytes(payload)
    source_order = "big" if flags & _BIG_ENDIAN else "little"
    if source_order != sys.byteorder:
        values.byteswap()
    if floating:
        values = array("h", (max(-32768, min(32767, round(max(-1.0, min(1.0, value)) * 32768)))
                            if math.isfinite(value) else 0 for value in values))
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def _sample_pcm16(sample, cm):
    if not cm.CMSampleBufferIsValid(sample) or not cm.CMSampleBufferDataIsReady(sample):
        return b""
    description = cm.CMAudioFormatDescriptionGetStreamBasicDescription(
        cm.CMSampleBufferGetFormatDescription(sample))
    if description is None:
        raise MacOSAudioError("Mac 系统音频缺少格式信息，请重新开始监听。")
    frame_count = int(cm.CMSampleBufferGetNumSamples(sample))
    if frame_count == 0:
        return b""
    block = cm.CMSampleBufferGetDataBuffer(sample)
    if block is None:
        raise MacOSAudioError("Mac 系统音频缓冲区为空，请重新开始监听。")
    size = int(cm.CMBlockBufferGetDataLength(block))
    # Check before asking the bridge to allocate its output byte buffer.
    if not 0 <= size <= RATE * 8 or not 0 <= frame_count <= RATE:
        raise MacOSAudioError("Mac 系统音频缓冲区长度异常，已停止监听。")
    status, payload = cm.CMBlockBufferCopyDataBytes(block, 0, size, None)
    if status:
        raise MacOSAudioError("无法读取 Mac 系统音频缓冲区，请重新开始监听。")
    return _pcm16_from_payload(bytes(payload), description, frame_count)


def _output_class(frameworks):
    global _handler_type
    with _handler_lock:
        if _handler_type is not None:
            return _handler_type
        objc = frameworks.objc

        class InterviewCopilotSystemAudioOutput(frameworks.Foundation.NSObject,
                protocols=[objc.protocolNamed("SCStreamOutput"),
                           objc.protocolNamed("SCStreamDelegate")]):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sample, kind):
                owner = getattr(self, "_owner", None)
                if owner is not None:
                    owner._receive(sample, kind)

            def stream_didStopWithError_(self, stream, error):
                owner = getattr(self, "_owner", None)
                if owner is not None:
                    owner._failed(MacOSAudioError(_error_text(error, "Mac 系统音频流已中断")))

        _handler_type = InterviewCopilotSystemAudioOutput
        return _handler_type


class _SystemAudioStream:
    def __init__(self, frameworks, callback, frames_per_buffer, cancel_event=None):
        self._f = frameworks
        self._callback = callback
        self._packet_frames = max(128, min(RATE, int(frames_per_buffer)))
        self._cancel = cancel_event or threading.Event()
        self._closed = threading.Event()
        self._active = False
        self._error = None
        self._native = self._handler = self._queue = None

    def _wait(self, invoke, *, timeout=15, late_cleanup=None):
        finished = threading.Event()
        abandoned = threading.Event()
        completion_lock = threading.Lock()
        result = []

        def complete(*values):
            with completion_lock:
                was_abandoned = abandoned.is_set()
                if not was_abandoned:
                    result[:] = values
                    finished.set()
            if was_abandoned and late_cleanup:
                late_cleanup()

        invoke(complete)
        deadline = time.monotonic() + timeout
        while not finished.wait(0.05):
            if self._cancel.is_set() or self._closed.is_set():
                with completion_lock:
                    abandoned.set()
                    already_finished = finished.is_set()
                if already_finished and late_cleanup:
                    late_cleanup()
                raise MacOSAudioError("Mac 系统声音监听已取消。")
            if time.monotonic() >= deadline:
                with completion_lock:
                    if finished.is_set():
                        break
                    abandoned.set()
                raise MacOSAudioError("等待 Mac 系统录制授权或启动超时。" + PERMISSION_HELP)
        if self._cancel.is_set() or self._closed.is_set():
            if late_cleanup:
                late_cleanup()
            raise MacOSAudioError("Mac 系统声音监听已取消。")
        return tuple(result)

    def start(self):
        if self._cancel.is_set():
            raise MacOSAudioError("Mac 系统声音监听已取消。")
        f = self._f
        with f.objc.autorelease_pool():
            # This call (only reached after Start) requests Screen Recording
            # permission when needed. No UI/page-load/device-list call does so.
            content, error = self._wait(lambda done:
                f.SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                    True, False, done))
            if error is not None or content is None:
                raise MacOSAudioError(_error_text(error, "无法获取 Mac 系统录制权限"))
            displays = content.displays()
            if not displays:
                raise MacOSAudioError("Mac 未发现可用显示器，请连接显示器后重新监听。")
            capture_filter = f.SCK.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
                displays[0], [], [])
            config = f.SCK.SCStreamConfiguration.alloc().init()
            config.setCapturesAudio_(True)
            config.setExcludesCurrentProcessAudio_(True)
            config.setSampleRate_(RATE)
            config.setChannelCount_(CHANNELS)
            if hasattr(config, "setCaptureMicrophone_"):
                config.setCaptureMicrophone_(False)
            config.setWidth_(2)
            config.setHeight_(2)
            config.setShowsCursor_(False)
            config.setQueueDepth_(3)
            config.setMinimumFrameInterval_(f.CM.CMTimeMake(1, 1))
            self._handler = _output_class(f).alloc().init()
            self._handler._owner = self
            self._queue = f.dispatch.dispatch_queue_create(b"cn.interviewcopilot.system-audio", None)
            native = self._native = f.SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
                capture_filter, config, self._handler)
            ok, error = native.addStreamOutput_type_sampleHandlerQueue_error_(
                self._handler, f.SCK.SCStreamOutputTypeAudio, self._queue, None)
            if not ok:
                raise MacOSAudioError(_error_text(error, "无法连接 Mac 系统音频流"))
            self._active = True
            (error,) = self._wait(native.startCaptureWithCompletionHandler_,
                late_cleanup=lambda: native.stopCaptureWithCompletionHandler_(lambda error: None))
            if error is not None:
                raise MacOSAudioError(_error_text(error, "无法启动 Mac 系统声音监听"))

    def _failed(self, error):
        if not self._closed.is_set() and not self._cancel.is_set():
            self._error = error
        self._active = False

    def _receive(self, sample, kind):
        if (not self._active or self._closed.is_set() or self._cancel.is_set() or self._error
                or kind != self._f.SCK.SCStreamOutputTypeAudio):
            return
        try:
            pcm = _sample_pcm16(sample, self._f.CM)
            step = self._packet_frames * 2
            for offset in range(0, len(pcm), step):
                if self._closed.is_set() or self._cancel.is_set():
                    break
                packet = pcm[offset:offset + step]
                _, status = self._callback(packet, len(packet) // 2, {}, 0)
                if status == paComplete:
                    self._active = False
                    break
        except Exception as exc:
            self._failed(exc if isinstance(exc, AudioError) else
                         MacOSAudioError("Mac 系统音频转换失败，请重新开始监听。"))

    def is_active(self):
        if self._error is not None:
            raise self._error
        return self._active and not self._closed.is_set() and not self._cancel.is_set()

    def stop_stream(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._active = False
        native = self._native
        if native is not None:
            done = threading.Event()
            try:
                native.stopCaptureWithCompletionHandler_(lambda error: done.set())
                done.wait(2)
            except Exception:
                pass
        if self._handler is not None:
            self._handler._owner = None
        self._native = self._handler = self._queue = None

    def close(self):
        self.stop_stream()


class PyAudio:
    """One output-only system mix; microphone identifiers are never accepted."""
    def __init__(self):
        self._frameworks = _load_frameworks()
        self._streams = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.terminate()

    def get_host_api_info_by_type(self, host_type):
        if host_type != paWASAPI:
            raise MacOSAudioError("Mac 版只支持 ScreenCaptureKit 系统播放音频。")
        return {"index": paWASAPI, "name": "ScreenCaptureKit"}

    def get_default_wasapi_loopback(self):
        return _device()

    def get_loopback_device_info_generator(self):
        yield _device()

    def get_device_info_by_index(self, device_id):
        if int(device_id) != 0:
            raise MacOSAudioError("Mac 版只能选择“系统播放声音”，不能选择麦克风；请刷新设备。")
        return _device()

    def open(self, *, format, channels, rate, input, input_device_index,
             frames_per_buffer, stream_callback, cancel_event=None):
        self.get_device_info_by_index(input_device_index)
        if format != paInt16 or channels != CHANNELS or rate != RATE or input is not True:
            raise MacOSAudioError("Mac 系统音频需要使用 16 kHz 单声道 PCM16 格式。")
        stream = _SystemAudioStream(self._frameworks, stream_callback,
                                    frames_per_buffer, cancel_event)
        self._streams.append(stream)
        try:
            stream.start()
        except BaseException:
            stream.close()
            self._streams.remove(stream)
            raise
        return stream

    def terminate(self):
        for stream in self._streams:
            stream.close()
        self._streams.clear()
