"""Tests for pipeline.video_ingest and the /upload HTTP endpoint."""

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
from pipeline import key_store, subtitle_extract, translator, video_ingest


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _make_sample_video(path):
    _require_tools()
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "color=c=black:s=320x240:d=1",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
    )


class VideoIngestModuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _require_tools()
        cls._tmp = tempfile.mkdtemp()
        cls.video_path = Path(cls._tmp) / "sample.mp4"
        _make_sample_video(cls.video_path)
        cls.upload_root = Path(cls._tmp) / "uploads"

    def test_validate_file_type_accepts_supported(self):
        for name in ("a.mp4", "b.mkv", "c.mov", "d.avi", "e.webm", "f.flv", "g.wmv", "h.m4v"):
            with self.subTest(name=name):
                video_ingest.validate_file_type(name)

    def test_validate_file_type_rejects_unsupported(self):
        for name in ("a.txt", "b.exe", "noext", "c.MPEG"):
            with self.subTest(name=name):
                with self.assertRaises(video_ingest.UnsupportedFileError):
                    video_ingest.validate_file_type(name)

    def test_probe_video_returns_metadata(self):
        meta = video_ingest.probe_video(self.video_path)
        self.assertIsNotNone(meta["duration_sec"])
        self.assertEqual(meta["width"], 320)
        self.assertEqual(meta["height"], 240)

    def test_probe_video_on_garbage_raises(self):
        bad = Path(self._tmp) / "bad.mp4"
        bad.write_bytes(b"this is not a video")
        with self.assertRaises(video_ingest.VideoProbeError):
            video_ingest.probe_video(bad)

    def test_create_job_builds_job_dir_and_meta(self):
        job = video_ingest.create_job(
            self.video_path, "sample.mp4", upload_root=self.upload_root
        )
        self.assertTrue(job["job_id"])
        job_dir = self.upload_root / job["job_id"]
        self.assertTrue((job_dir / "source.mp4").exists())
        self.assertEqual(job["source_filename"], "sample.mp4")
        self.assertEqual(job["duration_sec"], 1.0)
        self.assertEqual(job["width"], 320)
        self.assertEqual(job["height"], 240)
        meta_on_disk = json.loads(
            (job_dir / "job_meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(meta_on_disk["job_id"], job["job_id"])

    def test_create_job_rejects_unsupported_filename(self):
        with self.assertRaises(video_ingest.UnsupportedFileError):
            video_ingest.create_job(self.video_path, "sample.txt", upload_root=self.upload_root)

    def test_ensure_active_key_blocks_empty(self):
        with self.assertRaises(video_ingest.NoActiveKeyError):
            video_ingest.ensure_active_key([])
        video_ingest.ensure_active_key(["k1"])


class UploadEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _require_tools()
        cls._tmp = tempfile.mkdtemp()
        cls.video_path = Path(cls._tmp) / "sample.mp4"
        _make_sample_video(cls.video_path)
        cls.video_bytes = cls.video_path.read_bytes()

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp_dir) / "uploads"
        self.key_store_path = Path(self._tmp_dir) / "gemini_keys_store.json"
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_key_store_path = key_store.KEY_STORE_PATH
        video_ingest.UPLOAD_ROOT = self.upload_root
        key_store.KEY_STORE_PATH = self.key_store_path
        self.addCleanup(self._restore)
        self.client = TestClient(app)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        key_store.KEY_STORE_PATH = self._orig_key_store_path

    def _add_key(self):
        self.key_store_path.write_text(
            json.dumps({"keys": [{"id": "k1", "key": "test-key", "label": None}]}),
            encoding="utf-8",
        )

    def _upload(self, filename="sample.mp4", content=None):
        files = {"file": (filename, content if content is not None else self.video_bytes, "video/mp4")}
        return self.client.post("/upload", files=files)

    def _wait_for_upload_done(self, job_id, timeout=10.0, interval=0.1):
        """Poll the job status endpoint until the upload pipeline finishes.

        Since U1b the B1/B2/C1 chain runs in a background thread, so the
        /upload response no longer carries a "pipeline" key.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            body = self.client.get(f"/api/jobs/{job_id}/status").json()
            if body.get("state") == "done":
                return body
            if body.get("state") == "error":
                self.fail(f"upload pipeline errored: {body}")
            time.sleep(interval)
        self.fail(f"upload pipeline for {job_id} did not finish in {timeout}s")

    def test_upload_blocked_without_active_key(self):
        res = self._upload()
        self.assertEqual(res.status_code, 400)
        self.assertIn("Gemini API key", res.json()["detail"])
        self.assertFalse(self.upload_root.exists())

    def test_upload_success_creates_job(self):
        self._add_key()
        # /upload now chains B1 -> B2 -> C1 in a background thread; Gemini is
        # mocked so no network. The mocks must stay active until the job
        # reaches "done", so the upload + poll both live inside the with block.
        with mock.patch.object(
            subtitle_extract,
            "_call_gemini",
            return_value=[
                {"text": "你好", "start_sec": 0.0, "end_sec": 0.5},
            ],
        ), mock.patch.object(
            translator,
            "_call_gemini_text",
            return_value="नमस्ते",
        ):
            res = self._upload()
            self.assertEqual(res.status_code, 200)
            body = res.json()
            job_id = body["job_id"]
            self.assertTrue(job_id)
            self.assertEqual(body["status"], "processing")
            self.assertNotIn("pipeline", body)
            status = self._wait_for_upload_done(job_id)
        upload_stage = status["stages"]["upload_pipeline"]
        # G1 wiring: the upload endpoint must chain the subtitle pipeline so
        # the next phase has its inputs ready; the summary now comes from the
        # job status file rather than the response body.
        self.assertEqual(upload_stage["extraction_status"], "ok")
        self.assertEqual(upload_stage["serials"], 1)
        job_dir = self.upload_root / job_id
        self.assertTrue((job_dir / "source.mp4").exists())
        self.assertEqual(body["meta"]["source_filename"], "sample.mp4")
        self.assertEqual(body["meta"]["duration_sec"], 1.0)
        self.assertEqual(body["meta"]["width"], 320)
        self.assertEqual(body["meta"]["height"], 240)
        self.assertTrue((job_dir / "job_meta.json").exists())
        self.assertTrue((job_dir / "subtitles_zh_raw.json").exists())
        self.assertTrue((job_dir / "subtitles_zh.json").exists())
        self.assertTrue((job_dir / "subtitles_hi.json").exists())

    def test_upload_rejects_unsupported_file_type(self):
        self._add_key()
        res = self._upload(filename="notes.txt", content=b"not a video")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unsupported file type", res.json()["detail"])

    def test_upload_rejects_garbage_video(self):
        self._add_key()
        res = self._upload(filename="garbage.mp4", content=b"definitely not a real video")
        self.assertEqual(res.status_code, 400)
        self.assertIn("ffprobe", res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
