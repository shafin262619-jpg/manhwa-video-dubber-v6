"""Tests for pipeline.job_logging (U4 per-job persistent pipeline.log).

Confirms that ``get_job_logger`` appends to ``uploads/<job_id>/logs/pipeline.log``,
reuses a single handler on repeated calls (no duplicated log lines), keeps
separate files per job, and that real pipeline entry functions route their
progress into the job's log file.
"""

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from pipeline import job_logging, key_store, video_ingest
from pipeline import subtitle_extract, translator, voiceover_auto


class GetJobLoggerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-log-" + uuid.uuid4().hex[:8]
        self.log_path = self.upload_root / self.job_id / "logs" / "pipeline.log"

    def _log_lines(self):
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def test_writes_to_per_job_log_file(self):
        logger = job_logging.get_job_logger(self.job_id, upload_root=self.upload_root)
        logger.info("hello job %s", self.job_id)
        lines = self._log_lines()
        self.assertTrue(
            any(line.endswith(f": hello job {self.job_id}") for line in lines)
        )
        self.assertTrue(any("[INFO]" in line for line in lines))

    def test_repeated_calls_do_not_duplicate_handler_or_lines(self):
        first = job_logging.get_job_logger(self.job_id, upload_root=self.upload_root)
        first.info("first message")
        second = job_logging.get_job_logger(self.job_id, upload_root=self.upload_root)
        self.assertIs(first, second)
        self.assertEqual(len(second.handlers), 1)
        second.info("second message")
        lines = self._log_lines()
        self.assertEqual(
            sum(1 for line in lines if line.endswith(": first message")), 1
        )
        self.assertEqual(
            sum(1 for line in lines if line.endswith(": second message")), 1
        )

    def test_different_jobs_write_separate_files(self):
        other_id = self.job_id + "-2"
        job_logging.get_job_logger(self.job_id, upload_root=self.upload_root).info("job a")
        job_logging.get_job_logger(other_id, upload_root=self.upload_root).info("job b")
        a = (self.upload_root / self.job_id / "logs" / "pipeline.log")
        b = (self.upload_root / other_id / "logs" / "pipeline.log")
        a_text = a.read_text(encoding="utf-8")
        b_text = b.read_text(encoding="utf-8")
        self.assertIn(": job a", a_text)
        self.assertNotIn(": job b", a_text)
        self.assertIn(": job b", b_text)

    def test_uses_video_ingest_upload_root_by_default(self):
        logger = job_logging.get_job_logger(self.job_id)
        expected = video_ingest.UPLOAD_ROOT / self.job_id / "logs" / "pipeline.log"
        self.assertTrue(
            any(Path(h.baseFilename) == expected for h in logger.handlers)
        )


class MultiStageLogTest(unittest.TestCase):
    """Several pipeline stages append into one per-job pipeline.log file.

    Gemini keys are mocked to be empty so each stage takes its "cannot start"
    path — no network, no ffmpeg — while still writing its stage entry through
    the job logger (messages asserted below).
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-e2e"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        (self.job_dir / "source.mp4").write_bytes(b"dummy-video-bytes")
        (self.job_dir / "job_meta.json").write_text(
            json.dumps({"job_id": self.job_id, "duration_sec": 1.0}),
            encoding="utf-8",
        )
        entries = [
            {"serial": 1, "text_zh": "你好", "text_hi": "नमस्ते",
             "start_sec": 0.0, "end_sec": 1.0},
            {"serial": 2, "text_zh": "再见", "text_hi": "अलविदा",
             "start_sec": 1.0, "end_sec": 2.0},
        ]
        (self.job_dir / "subtitles_zh.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        (self.job_dir / "subtitles_hi.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        self._keys = mock.patch.object(key_store, "get_active_keys", return_value=[])
        self._keys.start()
        self.addCleanup(self._keys.stop)

    def _log_text(self):
        path = self.upload_root / self.job_id / "logs" / "pipeline.log"
        self.assertTrue(path.exists(), "pipeline.log was not created")
        return path.read_text(encoding="utf-8")

    def test_multiple_stages_append_to_same_pipeline_log(self):
        subtitle_extract.extract_subtitles(self.job_id, upload_root=self.upload_root)
        translator.translate_subtitles(self.job_id, upload_root=self.upload_root)
        voiceover_auto.generate_auto_voiceover(self.job_id, upload_root=self.upload_root)

        content = self._log_text()
        self.assertIn(
            "extraction cannot start for job job-e2e: no active Gemini keys", content
        )
        self.assertIn(
            "translation fallback for job job-e2e: no active Gemini keys", content
        )
        self.assertIn(
            "auto voiceover cannot start for job job-e2e: no active Gemini keys",
            content,
        )
        self.assertEqual(content.count("job job-e2e"), 3)
