"""Tests for pipeline.subtitle_verify (D1 independent Whisper cross-check).

Whisper is a heavy optional dependency and is not installed in this
environment, so every test injects a fake ``whisper`` module via
``sys.modules`` (mirroring the mocked-whisper style of
``test_voiceover_upload.py``) or pins ``sys.modules["whisper"] = None`` to
force the ImportError path.
"""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from pipeline import subtitle_verify


def _make_job_dir(upload_root, job_id):
    job_dir = upload_root / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "source.mp4").write_bytes(b"fake-source")
    return job_dir


def _write_qa(job_dir, covered_sec):
    (job_dir / "subtitle_qa.json").write_text(
        json.dumps({"covered_duration_sec": covered_sec}), encoding="utf-8"
    )


def _no_whisper_patch():
    # sys.modules["whisper"] = None makes "import whisper" raise ImportError.
    return mock.patch.dict(sys.modules, {"whisper": None})


def _fake_whisper_patch(transcribe_impl=None):
    fake = types.ModuleType("whisper")

    def _load_model(name):
        return types.SimpleNamespace(transcribe=transcribe_impl)

    fake.load_model = _load_model
    return mock.patch.dict(sys.modules, {"whisper": fake})


class WhisperCrossCheckBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-d1"
        self.job_dir = _make_job_dir(self.upload_root, self.job_id)

    def _check(self):
        return subtitle_verify.whisper_cross_check(
            self.job_id, upload_root=self.upload_root
        )

    def _read_output(self):
        path = self.job_dir / "subtitle_qa_whisper.json"
        return json.loads(path.read_text(encoding="utf-8"))


class WhisperNotInstalledTest(WhisperCrossCheckBase):
    def test_whisper_not_installed_returns_skipped(self):
        _write_qa(self.job_dir, 8.0)
        with _no_whisper_patch(), mock.patch.object(
            subtitle_verify, "_convert_to_wav"
        ) as convert:
            result = self._check()
        convert.assert_called_once()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "whisper_not_installed")
        self.assertFalse(result["mismatch"])
        saved = self._read_output()
        self.assertEqual(saved["status"], "skipped")
        self.assertEqual(saved["reason"], "whisper_not_installed")


class TranscriptionFailureTest(WhisperCrossCheckBase):
    def test_transcription_error_returns_skipped(self):
        _write_qa(self.job_dir, 8.0)

        def _boom(path):
            raise RuntimeError("transcribe blew up")

        with _fake_whisper_patch(_boom), mock.patch.object(
            subtitle_verify, "_convert_to_wav"
        ):
            result = self._check()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "transcription_failed")
        self.assertFalse(result["mismatch"])

    def test_audio_extraction_failure_returns_skipped(self):
        _write_qa(self.job_dir, 8.0)
        with mock.patch.object(
            subtitle_verify,
            "_convert_to_wav",
            side_effect=RuntimeError("ffmpeg missing"),
        ):
            result = self._check()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "transcription_failed")


class CoverageResultTest(WhisperCrossCheckBase):
    SEGMENTS = [
        {"start": 0.0, "end": 4.0},
        {"start": 4.0, "end": 8.0},
        {"start": 8.0, "end": 12.0},
    ]

    def _transcribe(self, path):
        return {"segments": list(self.SEGMENTS)}

    def test_ratio_above_threshold_is_ok(self):
        _write_qa(self.job_dir, 10.0)
        with _fake_whisper_patch(self._transcribe), mock.patch.object(
            subtitle_verify, "_convert_to_wav"
        ):
            result = self._check()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["mismatch"])
        self.assertIsNone(result["reason"])
        self.assertAlmostEqual(result["whisper_spoken_sec"], 12.0, places=3)
        self.assertAlmostEqual(result["extracted_covered_sec"], 10.0, places=3)
        self.assertAlmostEqual(result["coverage_ratio"], 10.0 / 12.0, places=4)

    def test_ratio_below_threshold_is_mismatch(self):
        _write_qa(self.job_dir, 8.0)
        with _fake_whisper_patch(self._transcribe), mock.patch.object(
            subtitle_verify, "_convert_to_wav"
        ):
            result = self._check()
        self.assertEqual(result["status"], "mismatch")
        self.assertTrue(result["mismatch"])
        self.assertAlmostEqual(result["coverage_ratio"], 8.0 / 12.0, places=4)

    def test_output_json_written_with_full_dict(self):
        _write_qa(self.job_dir, 10.0)
        with _fake_whisper_patch(self._transcribe), mock.patch.object(
            subtitle_verify, "_convert_to_wav"
        ):
            self._check()
        saved = self._read_output()
        for key in (
            "status",
            "reason",
            "whisper_spoken_sec",
            "extracted_covered_sec",
            "coverage_ratio",
            "mismatch",
        ):
            self.assertIn(key, saved)
        self.assertEqual(saved["status"], "ok")
        self.assertAlmostEqual(saved["whisper_spoken_sec"], 12.0, places=3)
        self.assertAlmostEqual(saved["extracted_covered_sec"], 10.0, places=3)


if __name__ == "__main__":
    unittest.main()
