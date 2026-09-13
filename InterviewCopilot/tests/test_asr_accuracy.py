"""Recognition policy and signal regressions; no ML model download or capture."""
from array import array
from io import BytesIO
import math
import sys
from types import SimpleNamespace
import unittest
import wave

from interview_copilot.asr_worker import simplify_chinese, transcribe_phrase
from interview_copilot.audio import EnergySegmenter, _wav_bytes
from interview_copilot.local_asr import LocalASRError, prepare_local_wave, recognition_options
from tests.test_audio import RATE, feed_ms, silence, tone


class RecognitionPolicyTests(unittest.TestCase):
    def test_quality_beams_preserve_speech_filter_and_disable_previous_text(self):
        for quality, beam in (("fast", 1), ("balanced", 3), ("accurate", 5)):
            options = recognition_options(quality)
            self.assertEqual(options["beam_size"], beam)
            self.assertTrue(options["vad_filter"])
            self.assertFalse(options["condition_on_previous_text"])
            self.assertNotIn("hotwords", options)
        with self.assertRaises(LocalASRError):
            recognition_options("unbounded")

    @unittest.skipUnless(sys.platform == "win32", "Windows script conversion")
    def test_chinese_script_conversion_preserves_english_and_unicode(self):
        self.assertEqual(simplify_chinese("客戶留存率，項目管理 CRM 🙂"), "客户留存率，项目管理 CRM 🙂")


class SignalAccuracyTests(unittest.TestCase):
    def test_opposite_phase_stereo_is_not_cancelled_before_whisper(self):
        wav, meta = prepare_local_wave(_wav_bytes(tone(400, channels=2), RATE, 2))
        with wave.open(BytesIO(wav), "rb") as source:
            self.assertEqual(source.getnchannels(), 1)
            samples = array("h", source.readframes(source.getnframes()))
        self.assertGreater(max(abs(v) for v in samples), 9000)
        self.assertTrue(meta["has_signal"])

    def test_silence_and_low_background_do_not_get_amplified(self):
        for pcm in (silence(300), tone(300, amplitude=10), tone(300, amplitude=150)):
            _, meta = prepare_local_wave(_wav_bytes(pcm, RATE, 1))
            self.assertEqual(meta["gain"], 1)
        _, meta = prepare_local_wave(_wav_bytes(silence(300), RATE, 1))
        self.assertFalse(meta["has_signal"])

    def test_dc_offset_cannot_become_a_fake_speech_signal(self):
        pcm = array("h", [1000] * 4800).tobytes()
        _, meta = prepare_local_wave(_wav_bytes(pcm, RATE, 1))
        self.assertFalse(meta["has_signal"])

    def test_channel_fix_preserves_source_volume(self):
        pulse = array("h", [0] * 90 + [1000, -1000] * 5).tobytes() * 48
        wav, meta = prepare_local_wave(_wav_bytes(pulse, RATE, 1))
        self.assertEqual(meta["gain"], 1)
        with wave.open(BytesIO(wav), "rb") as source:
            values = array("h", source.readframes(source.getnframes()))
        self.assertLess(max(abs(v) for v in values), 32767)

    def test_quiet_final_syllables_are_not_trimmed_as_silence(self):
        detector = EnergySegmenter(RATE, 1, energy_threshold=.008)
        self.assertEqual(feed_ms(detector, 400, True), [])
        for _ in range(40):
            self.assertEqual(detector.feed(tone(20, amplitude=280)), [])
        chunks = feed_ms(detector, 700, False)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), round(RATE * 1.35) * 2)

    def test_one_active_stereo_channel_has_same_detection_level_as_mono(self):
        mono = array("h", tone(20, amplitude=180))
        stereo = array("h", (value for sample in mono for value in (sample, 0)))
        first, second = EnergySegmenter(RATE, 1), EnergySegmenter(RATE, 2)
        first.feed(mono.tobytes())
        second.feed(stereo.tobytes())
        self.assertAlmostEqual(first.level, second.level)


class FakeWhisper:
    def __init__(self, detected="zh", probabilities=None, duration=1.0):
        self.detected = detected
        self.probabilities = probabilities or [("zh", .96), ("en", .02)]
        self.duration = duration
        self.calls, self.decoded = [], []

    def transcribe(self, source, *, language, **options):
        self.calls.append({"language": language, **options})
        selected = language or self.detected
        info = SimpleNamespace(language=selected, all_language_probs=self.probabilities,
                               duration_after_vad=self.duration)
        def decode():
            self.decoded.append(selected)
            yield SimpleNamespace(text="客户留存率", avg_logprob=-.2)
        return decode(), info


class PhraseTests(unittest.TestCase):
    def test_silence_never_reaches_model(self):
        model = FakeWhisper()
        result = transcribe_phrase(model, _wav_bytes(silence(1000), RATE, 1))
        self.assertEqual(result["text"], "")
        self.assertEqual(model.calls, [])

    def test_vad_rejected_noise_never_decodes(self):
        model = FakeWhisper(duration=0)
        result = transcribe_phrase(model, _wav_bytes(tone(400), RATE, 1))
        self.assertEqual(result["text"], "")
        self.assertEqual(model.decoded, [])

    def test_auto_uses_model_language_without_custom_heuristics(self):
        model = FakeWhisper("en", [("en", .95), ("zh", .04)])
        result = transcribe_phrase(model, _wav_bytes(tone(400), RATE, 1), quality="accurate")
        self.assertEqual([call["language"] for call in model.calls], [None])
        self.assertEqual(model.decoded, ["en"])
        self.assertEqual(result["metadata"]["language_probability"], .95)
        self.assertEqual(model.calls[-1]["beam_size"], 5)

    def test_explicit_chinese_skips_auto_detection(self):
        model = FakeWhisper()
        result = transcribe_phrase(model, _wav_bytes(tone(400), RATE, 1), language="zh")
        self.assertEqual([call["language"] for call in model.calls], ["zh"])
        self.assertIsNone(result["metadata"]["language_probability"])
        self.assertTrue(result["metadata"]["language_forced"])


if __name__ == "__main__":
    unittest.main()
