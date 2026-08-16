"""Tests for pipeline.error_bn (F11: Bengali error messages).

One case per mapped exception family, mirroring ``app._friendly_error``:
CallBudgetExceeded, AllKeysExhausted (empty + populated), ffmpeg/subprocess
failures, whisper import/runtime errors, malformed-transcript placeholder
(F12), timeouts/network, and the generic fallback that names the stage.
Also verifies the mapper never raises, even on pathological exceptions.
"""

import json
import subprocess
import unittest

from pipeline import error_bn, gemini_rotation


def _is_bn(text):
    """True when ``text`` contains at least one Bengali script character."""
    return any(0x0980 <= ord(ch) <= 0x09FF for ch in text)


class ExplainBnTest(unittest.TestCase):
    def test_call_budget_exceeded(self):
        exc = gemini_rotation.CallBudgetExceeded(5, 10)
        msg = error_bn.explain_bn(exc, "C1_translate")
        self.assertIn("5", msg)
        self.assertIn("10", msg)
        self.assertIn("Gemini", msg)
        self.assertTrue(_is_bn(msg))

    def test_all_keys_exhausted_with_attempts(self):
        exc = gemini_rotation.AllKeysExhausted([(1, "quota exceeded")])
        msg = error_bn.explain_bn(exc, "D2_voiceover")
        self.assertIn("1", msg)
        self.assertIn("quota", msg)
        self.assertTrue(_is_bn(msg))

    def test_all_keys_exhausted_none_configured(self):
        exc = gemini_rotation.AllKeysExhausted([])
        msg = error_bn.explain_bn(exc)
        self.assertIn("key", msg)
        self.assertTrue(_is_bn(msg))

    def test_ffmpeg_runtime_error(self):
        msg = error_bn.explain_bn(RuntimeError("ffmpeg error: broken pipe"), "E2_draft")
        self.assertIn("ffmpeg", msg)
        self.assertTrue(_is_bn(msg))

    def test_ffprobe_failure(self):
        msg = error_bn.explain_bn(RuntimeError("ffprobe failed: no such file"), "F1_extract")
        self.assertIn("ffmpeg", msg)

    def test_subprocess_called_process_error(self):
        exc = subprocess.CalledProcessError(returncode=1, cmd=["ffmpeg", "-y"])
        msg = error_bn.explain_bn(exc, "F3_final")
        self.assertIn("ffmpeg", msg)
        self.assertTrue(_is_bn(msg))

    def test_whisper_import_error(self):
        exc = ImportError("No module named 'whisper'")
        msg = error_bn.explain_bn(exc, "F1_extract")
        self.assertIn("Whisper", msg)
        self.assertTrue(_is_bn(msg))

    def test_whisper_runtime_error(self):
        exc = RuntimeError("whisper load_model failed on this device")
        msg = error_bn.explain_bn(exc, "D3_align")
        self.assertIn("Whisper", msg)

    def test_invalid_transcript_format_placeholder(self):
        exc = json.JSONDecodeError("Expecting value", "doc", 0)
        msg = error_bn.explain_bn(exc, "F1_extract")
        self.assertIn("F12", msg)
        self.assertTrue(_is_bn(msg))

    def test_timeout_error(self):
        exc = TimeoutError("timed out after 300s")
        msg = error_bn.explain_bn(exc, "C1_translate")
        self.assertIn("টাইমআউট", msg)

    def test_networkish_message(self):
        exc = RuntimeError("connection reset by peer")
        msg = error_bn.explain_bn(exc, "C1_translate")
        self.assertIn("নেটওয়ার্ক", msg)

    def test_generic_fallback_names_bengali_stage(self):
        msg = error_bn.explain_bn(ValueError("something odd happened"), "C1_translate")
        self.assertIn("অনুবাদ হচ্ছে", msg)
        self.assertIn("something odd happened", msg)

    def test_generic_fallback_names_umbrella_stage(self):
        msg = error_bn.explain_bn(ValueError("boom"), "final_render")
        self.assertIn("ফাইনাল রেন্ডার", msg)

    def test_generic_fallback_unknown_stage(self):
        msg = error_bn.explain_bn(ValueError("boom"), "weird_stage_name")
        self.assertIn("weird_stage_name", msg)

    def test_long_error_truncated(self):
        long_msg = "x" * 1000
        msg = error_bn.explain_bn(ValueError(long_msg), "E1_guideline")
        self.assertLess(len(msg), 360)

    def test_never_raises_on_pathological_exception(self):
        class Weird:
            def __str__(self):
                raise RuntimeError("boom in str()")

        msg = error_bn.explain_bn(Weird(), "F1_extract")
        self.assertIn("অজানা", msg)

    def test_never_raises_on_none(self):
        msg = error_bn.explain_bn(None, "F1_extract")
        self.assertIsInstance(msg, str)
        self.assertTrue(msg)


if __name__ == "__main__":
    unittest.main()
