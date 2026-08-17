"""Tests for F12c Part A: resumable upload_pipeline (sub-stage granularity).

Covers the artifact-presence derivation of upload sub-stage resume points
(``resume.find_upload_resume_point``) and the HTTP wiring: an interrupted
upload pipeline resumes from its first missing sub-stage (F1/B2/whisper/C1),
completed sub-stages are never re-run, both ``subtitle_source`` paths are
resumable, gap-fill stats survive a resume, and nothing-done jobs keep the
409.

The resume simulation mirrors ``test_resume.py``: jobs are built on disk with
only some artifacts present, then ``POST /jobs/{id}/resume`` is driven through
the app with Gemini/Whisper stages mocked.
"""

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import (
    job_config,
    key_store,
    render_final,
    resume,
    subtitle_builder,
    subtitle_extract,
    subtitle_verify,
    transcript_import,
    translator,
    video_ingest,
    voiceover_auto,
)


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


class FindUploadResumePointTest(unittest.TestCase):
    """Unit coverage of the sub-stage artifact derivation."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore_paths)
        self.job_dir = self.upload_root / "job-u"
        self.job_dir.mkdir(parents=True)

    def _restore_paths(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def test_nothing_started_reports_upload_pipeline(self):
        self.assertEqual(
            resume.find_upload_resume_point("job-u"), "upload_pipeline"
        )

    def test_f1_done_resumes_from_b2(self):
        (self.job_dir / "subtitles_zh_raw.json").write_text(
            json.dumps({"status": "ok"}), encoding="utf-8"
        )
        self.assertEqual(resume.find_upload_resume_point("job-u"), "upload_B2")

    def test_f1_and_b2_done_resumes_from_whisper(self):
        (self.job_dir / "subtitles_zh_raw.json").write_text("{}", encoding="utf-8")
        (self.job_dir / "subtitles_zh.json").write_text("[]", encoding="utf-8")
        self.assertEqual(
            resume.find_upload_resume_point("job-u"), "upload_whisper"
        )

    def test_whisper_done_resumes_from_c1(self):
        (self.job_dir / "subtitles_zh_raw.json").write_text("{}", encoding="utf-8")
        (self.job_dir / "subtitles_zh.json").write_text("[]", encoding="utf-8")
        (self.job_dir / "subtitle_qa_whisper.json").write_text(
            json.dumps({"status": "ok"}), encoding="utf-8"
        )
        self.assertEqual(resume.find_upload_resume_point("job-u"), "upload_C1")

    def test_fully_complete_reports_none(self):
        (self.job_dir / "subtitles_zh_raw.json").write_text("{}", encoding="utf-8")
        (self.job_dir / "subtitles_zh.json").write_text("[]", encoding="utf-8")
        (self.job_dir / "subtitle_qa_whisper.json").write_text(
            json.dumps({"status": "ok"}), encoding="utf-8"
        )
        (self.job_dir / "subtitles_hi.json").write_text("[]", encoding="utf-8")
        self.assertIsNone(resume.find_upload_resume_point("job-u"))

    def test_missing_job_dir_reports_upload_pipeline(self):
        self.assertEqual(
            resume.find_upload_resume_point("no-such-job"), "upload_pipeline"
        )


class UploadPipelineResumeHttpTest(unittest.TestCase):
    """HTTP-level: POST /jobs/{id}/resume for interrupted upload pipelines."""

    def setUp(self):
        _require_tools()
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.key_store_path = Path(self._tmp) / "gemini_keys_store.json"
        self.output_root = Path(self._tmp) / "outputs"
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_key_store = key_store.KEY_STORE_PATH
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        key_store.KEY_STORE_PATH = self.key_store_path
        render_final.OUTPUT_ROOT = self.output_root
        self.addCleanup(self._restore_paths)

        self.client = TestClient(app)
        self.client.post("/settings/keys", data={"key": "test-gemini-key"})
        self._mocks = []
        self.addCleanup(self._stop_mocks)

    def _restore_paths(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        key_store.KEY_STORE_PATH = self._orig_key_store
        render_final.OUTPUT_ROOT = self._orig_output_root

    def _stop_mocks(self):
        for patch in self._mocks:
            patch.stop()

    def _patch(self, target, attribute, **kwargs):
        patch = mock.patch.object(target, attribute, **kwargs)
        patch.start()
        self._mocks.append(patch)
        return getattr(target, attribute)

    def _make_job(self, job_id, subtitle_source="gemini_extract", artifacts=()):
        job_dir = self.upload_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job_config.write_config(
            job_id, voice_source="user_upload", subtitle_source=subtitle_source
        )
        if "raw" in artifacts:
            (job_dir / "subtitles_zh_raw.json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "status": "ok",
                        "subtitles": [
                            {"text": "你好", "start_sec": 0.0, "end_sec": 1.5},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        if "zh" in artifacts:
            (job_dir / "subtitles_zh.json").write_text(
                json.dumps(
                    [
                        {"text_zh": "你好", "status": "ok",
                         "start_sec": 0.0, "end_sec": 1.5},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        if "whisper" in artifacts:
            (job_dir / "subtitle_qa_whisper.json").write_text(
                json.dumps({"status": "ok"}), encoding="utf-8"
            )
        return job_dir

    def _wait_upload_done(self, job_id, timeout=20.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.client.get(f"/api/jobs/{job_id}/status").json()
            entry = last.get("stages", {}).get("upload_pipeline", {})
            if entry.get("state") == "done":
                return last
            if entry.get("state") == "error":
                self.fail(f"upload_pipeline errored: {last}")
            time.sleep(0.1)
        self.fail(f"upload_pipeline not done within {timeout}s; last={last}")

    def _resume(self, job_id):
        return self.client.post(f"/jobs/{job_id}/resume")

    def test_resume_mid_upload_resumes_from_c1_only(self):
        # Interrupted right before C1: F1/B2/whisper artifacts exist, hi missing.
        self._make_job("job-c1", artifacts=("raw", "zh", "whisper"))
        extract = self._patch(subtitle_extract, "extract_subtitles")
        build = self._patch(subtitle_builder, "build_subtitle_list")
        whisper = self._patch(
            subtitle_verify, "whisper_cross_check", return_value={"status": "ok"}
        )
        translate = self._patch(
            translator, "translate_subtitles",
            return_value=[{"serial": 1, "text_zh": "你好", "text_hi": "नमस्ते"}],
        )

        res = self._resume("job-c1")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["resume_point"], "upload_C1")

        self._wait_upload_done("job-c1")
        translate.assert_called_once_with("job-c1", call_budget=mock.ANY)
        extract.assert_not_called()
        build.assert_not_called()
        whisper.assert_not_called()

    def test_resume_mid_upload_resumes_from_b2(self):
        # Interrupted after F1: only raw exists; B2/whisper/C1 must run once.
        self._make_job("job-b2", artifacts=("raw",))
        extract = self._patch(subtitle_extract, "extract_subtitles")
        build = self._patch(subtitle_builder, "build_subtitle_list")
        whisper = self._patch(
            subtitle_verify, "whisper_cross_check", return_value={"status": "ok"}
        )
        translate = self._patch(
            translator, "translate_subtitles",
            return_value=[{"serial": 1, "text_zh": "你好", "text_hi": "नमस्ते"}],
        )

        res = self._resume("job-b2")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["resume_point"], "upload_B2")

        self._wait_upload_done("job-b2")
        build.assert_called_once()
        whisper.assert_called_once()
        translate.assert_called_once()
        extract.assert_not_called()

    def test_resume_mid_upload_user_transcript_path(self):
        # user_transcript path: import + gap-fill live inside F1 and must NOT
        # re-run when subtitles_zh_raw.json already exists.
        self._make_job(
            "job-ut", subtitle_source="user_transcript", artifacts=("raw", "zh", "whisper")
        )
        importer = self._patch(transcript_import, "import_transcript")
        fill = self._patch(transcript_import, "fill_gaps")
        build = self._patch(subtitle_builder, "build_subtitle_list")
        whisper = self._patch(
            subtitle_verify, "whisper_cross_check", return_value={"status": "ok"}
        )
        translate = self._patch(
            translator, "translate_subtitles",
            return_value=[{"serial": 1, "text_zh": "你好", "text_hi": "नमस्ते"}],
        )

        res = self._resume("job-ut")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["resume_point"], "upload_C1")

        self._wait_upload_done("job-ut")
        translate.assert_called_once()
        importer.assert_not_called()
        fill.assert_not_called()
        build.assert_not_called()
        whisper.assert_not_called()

    def test_gap_fill_warning_survives_resume(self):
        # F12b Part C warning is restored from the persisted sidecar when the
        # gap-fill sub-stage itself is never re-run.
        job_dir = self._make_job(
            "job-gap", subtitle_source="user_transcript",
            artifacts=("raw", "zh", "whisper"),
        )
        (job_dir / "gap_fill_stats.json").write_text(
            json.dumps(
                {
                    "detected": 1,
                    "attempted": 1,
                    "filled": 0,
                    "failed": 1,
                    "added_entries": 0,
                    "windows": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._patch(
            translator, "translate_subtitles",
            return_value=[{"serial": 1, "text_zh": "你好", "text_hi": "नमस्ते"}],
        )

        res = self._resume("job-gap")
        self.assertEqual(res.status_code, 200, res.text)

        body = self._wait_upload_done("job-gap")
        stage = body["stages"]["upload_pipeline"]
        self.assertEqual(stage["gap_fill_stats"]["detected"], 1)
        self.assertEqual(stage["gap_fill_stats"]["failed"], 1)
        self.assertIn("gap_fill_warning_bn", stage)

    def test_resume_nothing_done_returns_409(self):
        job_dir = self.upload_root / "job-early"
        job_dir.mkdir(parents=True)
        res = self._resume("job-early")
        self.assertEqual(res.status_code, 409)
        self.assertIn("not finished", res.text)
        self.assertIn("nothing to resume", res.text)

    def test_resume_corrupted_raw_errors_gracefully_in_bengali(self):
        # A corrupted F1 artifact must surface a (Bengali) error, never a
        # silent restart from scratch. The real build_subtitle_list reads the
        # corrupted raw and raises -> upload_pipeline error with a Bengali
        # detail.
        job_dir = self._make_job("job-bad", artifacts=())
        (job_dir / "subtitles_zh_raw.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        self._patch(
            subtitle_verify, "whisper_cross_check", return_value={"status": "ok"}
        )
        self._patch(
            translator, "translate_subtitles",
            return_value=[{"serial": 1, "text_zh": "你好", "text_hi": "नमस्ते"}],
        )

        res = self._resume("job-bad")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["resume_point"], "upload_B2")

        deadline = time.time() + 20.0
        while time.time() < deadline:
            body = self.client.get(f"/api/jobs/job-bad/status").json()
            entry = body.get("stages", {}).get("upload_pipeline", {})
            if entry.get("state") == "error":
                break
            time.sleep(0.1)
        else:
            self.fail("upload_pipeline did not error for corrupted raw")
        self.assertTrue(entry.get("detail_bn"), "expected a Bengali detail")


if __name__ == "__main__":
    unittest.main()
