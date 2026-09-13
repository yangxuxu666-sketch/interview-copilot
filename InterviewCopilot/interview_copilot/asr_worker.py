"""Private JSON-lines ASR child. Never import this into the application server."""
import ctypes

# Set before importing any native audio/ML library. A child access violation
# must yield an exit code instead of an interactive Windows error dialog.
if __name__ == "__main__" and hasattr(ctypes, "windll"):
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)

import base64
from contextlib import redirect_stdout
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import wave

if __package__:
    from .local_asr import prepare_local_wave, recognition_options
else:
    from local_asr import prepare_local_wave, recognition_options


def _silence():
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    return buffer.getvalue()


def simplify_chinese(text):
    """A deterministic script conversion, not a model-based transcript rewrite."""
    if not text or not hasattr(ctypes, "windll"):
        return text
    convert = ctypes.windll.kernel32.LCMapStringEx
    convert.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_wchar_p,
        ctypes.c_int, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_ssize_t]
    convert.restype = ctypes.c_int
    required = convert("zh-CN", 0x02000000, text, -1, None, 0, None, None, 0)
    if not required:
        return text
    output = ctypes.create_unicode_buffer(required)
    if convert("zh-CN", 0x02000000, text, -1, output, required, None, None, 0):
        return output.value.replace("麽", "么")
    return text


def transcribe_phrase(model, wav, *, language=None, quality="balanced"):
    wav, signal = prepare_local_wave(wav)
    empty = {"ok": True, "text": "", "metadata": signal}
    if not signal["has_signal"]:
        return empty
    if language not in {None, "zh", "en"}:
        raise ValueError("invalid language")
    options = recognition_options(quality)
    segments, info = model.transcribe(BytesIO(wav), language=language, **options)
    # Whisper exposes language/VAD info before its lazy decoder runs. Do not
    # run text decoding for discarded silence.
    if info.duration_after_vad < 0.12:
        return empty
    chosen = info.language
    probability = dict(info.all_language_probs or []).get(chosen) if language is None else None
    decoded = list(segments)
    text = "".join(segment.text for segment in decoded).strip()
    if chosen == "zh":
        text = simplify_chinese(text)
    metadata = {**signal, "language": chosen, "language_probability": probability,
                "quality": quality, "language_forced": language is not None}
    if decoded:
        metadata["avg_logprob"] = round(sum(segment.avg_logprob for segment in decoded) / len(decoded), 3)
    return {"ok": True, "text": text, "metadata": metadata}


def main():
    output = sys.stdout.buffer
    model = None
    for line in sys.stdin.buffer:
        stage = "protocol"
        try:
            command = json.loads(line)
            with redirect_stdout(sys.stderr):
                if command["op"] == "init":
                    stage = "runtime"
                    if __package__:
                        from .windows_runtime import prepare_windows_runtime
                    else:
                        from windows_runtime import prepare_windows_runtime
                    prepare_windows_runtime(Path(command["model_dir"]).parent)
                    stage = "dependency"
                    from faster_whisper import WhisperModel
                    stage = "load"
                    if command["name"] not in {"tiny", "base", "small"}:
                        raise ValueError("invalid model")
                    model_dir = Path(command["model_dir"])
                    model_dir.mkdir(parents=True, exist_ok=True)
                    model = WhisperModel(command["name"], device="cpu", compute_type="int8",
                        cpu_threads=max(1, min(8, (os.cpu_count() or 4) - 1)),
                        num_workers=1, download_root=str(model_dir))
                    segments, _ = model.transcribe(BytesIO(_silence()), language=None,
                        beam_size=1, vad_filter=True, condition_on_previous_text=False)
                    list(segments)  # Initialize lazy VAD/preprocessing before reporting ready.
                    result = {"ok": True}
                elif command["op"] == "transcribe" and model is not None:
                    stage = "transcribe"
                    wav = base64.b64decode(command["wav"], validate=True)
                    result = transcribe_phrase(model, wav,
                        language=command.get("language"), quality=command.get("quality", "balanced"))
                else:
                    raise ValueError("invalid command")
        except Exception:
            # No exception repr, audio content, filesystem paths or credentials
            # are echoed into the application's user-visible error channel.
            result = {"ok": False, "error": stage}
        output.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
        output.flush()
        if not result["ok"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
