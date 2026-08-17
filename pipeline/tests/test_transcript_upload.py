"""HTTP-level tests for F12a: original-language transcript upload.

Covers the /upload route (optional transcript field, malformed rejection with
nothing persisted) and the background upload chain (F1 skipped when a
transcript is present, F1 still runs when absent).
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
    subtitle_extract,
    translator,
    video_ingest,
    voiceover_auto,
)

SRT_SAMPLE = """\
1
00:00:01,000 --> 00:00:03,500
আমার নাম জন

2
00:00:04,000 --> 00:00:06,500
আমি এখানে থাকি
"""


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _make_sample_video(path):
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "color=c=black:s=320x240:d=5",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
    )


class TranscriptUploadHttpTest(unittest.TestCase):
    """F12a /upload: transcript field, validation, F1 skipping."""

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

        video_path = Path(self._tmp) / "sample.mp4"
        _make_sample_video(video_path)
        self.video_bytes = video_path.read_bytes()

        silence_path = Path(self._tmp) / "silence.wav"
        voiceover_auto._make_silence(1.0, silence_path)
        self.silence_bytes = silence_path.read_bytes()

        self.client = TestClient(app)
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
        started = patch.start()
        self._mocks.append(patch)
        return started

    def _add_key(self):
        res = self.client.post("/settings/keys", data={"key": "test-gemini-key"})
        self.assertEqual(res.status_code, 200, res.text)

    def _upload(self, transcript=None, voice_source="user_upload",
                subtitle_source=None, target_lang=None):
        files = {"file": ("sample.mp4", self.video_bytes, "video/mp4")}
        if transcript is not None:
            files["transcript"] = transcript
        data = {"voice_source": voice_source}
        if subtitle_source is not None:
            data["subtitle_source"] = subtitle_source
        if target_lang is not None:
            data["target_lang"] = target_lang
        return self.client.post("/upload", data=data, files=files)

    def _wait_upload_done(self, job_id, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            body = self.client.get(f"/api/jobs/{job_id}/status").json()
            stage = (body.get("stages") or {}).get("upload_pipeline")
            if stage and stage.get("state") == "done":
                return body
            if stage and stage.get("state") == "error":
                self.fail(f"upload_pipeline errored: {body}")
            time.sleep(0.1)
        self.fail(f"upload_pipeline for {job_id} did not finish in {timeout}s")

    def test_transcript_upload_skips_f1_and_imports_transcript(self):
        self._add_key()
        extract = self._patch(
            subtitle_extract, "extract_subtitles",
            return_value={"status": "ok", "errors": {}},
        )
        self._patch(translator, "_call_gemini_text", return_value="नमस्ते")

        res = self._upload(
            transcript=("subs.srt", SRT_SAMPLE.encode("utf-8"), "application/x-subrip"),
            subtitle_source="user_transcript",
        )
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]

        self._wait_upload_done(job_id)
        job_dir = self.upload_root / job_id

        # F1 (Gemini extraction) must never have run.
        extract.assert_not_called()

        # The imported transcript is the F1-equivalent output.
        raw = json.loads(
            (job_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )
        self.assertEqual(raw["status"], "ok")
        self.assertFalse(raw["chunked"])
        self.assertEqual(raw["segments_count"], 1)
        self.assertEqual(raw["failed_segments"], [])
        self.assertEqual(raw["errors"], {})
        self.assertEqual(
            [s["text"] for s in raw["subtitles"]],
            ["আমার নাম জন", "আমি এখানে থাকি"],
        )
        self.assertEqual(raw["subtitles"][0]["start_sec"], 1.0)
        self.assertEqual(raw["subtitles"][0]["end_sec"], 3.5)
        self.assertFalse(raw["whisper_used"])

        # Downstream ran unchanged: B2 + C1 produced subtitles_zh.json and
        # subtitles_hi.json.
        for name in ("subtitles_zh.json", "subtitles_hi.json"):
            self.assertTrue((job_dir / name).exists(), f"missing {name}")

        cfg = job_config.read_config(job_id)
        self.assertEqual(cfg["subtitle_source"], "user_transcript")
        self.assertEqual(cfg["voice_source"], "user_upload")

    def test_no_transcript_keeps_gemini_f1_extraction(self):
        self._add_key()
        self._patch(
            subtitle_extract,
            "_call_gemini",
            return_value=[
                {"text": "你好", "start_sec": 0.0, "end_sec": 1.5},
            ],
        )
        self._patch(translator, "_call_gemini_text", return_value="नमस्ते")

        res = self._upload()
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]

        self._wait_upload_done(job_id)
        job_dir = self.upload_root / job_id

        raw = json.loads(
            (job_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )
        self.assertEqual(raw["status"], "ok")
        self.assertEqual([s["text"] for s in raw["subtitles"]], ["你好"])
        for name in ("subtitles_zh.json", "subtitles_hi.json"):
            self.assertTrue((job_dir / name).exists(), f"missing {name}")

        cfg = job_config.read_config(job_id)
        self.assertEqual(cfg["subtitle_source"], "gemini_extract")

    def test_malformed_transcript_rejected_with_nothing_saved(self):
        self._add_key()
        res = self._upload(
            transcript=("broken.srt", b"not a subtitle file", "application/x-subrip"),
            subtitle_source="user_transcript",
        )
        self.assertEqual(res.status_code, 400, res.text)
        detail = res.json()["detail"]
        self.assertIn("ট্রান্সক্রিপ্ট", detail)
        # Nothing persisted: no job dir, no history index, no partial state.
        self.assertEqual(list(self.upload_root.iterdir()) if self.upload_root.exists() else [], [])

    def test_empty_transcript_rejected_with_nothing_saved(self):
        self._add_key()
        res = self._upload(
            transcript=("empty.txt", b"   \n  \n", "text/plain"),
            subtitle_source="user_transcript",
        )
        self.assertEqual(res.status_code, 400, res.text)
        self.assertIn("ট্রান্সক্রিপ্ট", res.json()["detail"])
        self.assertEqual(list(self.upload_root.iterdir()) if self.upload_root.exists() else [], [])

    def test_transcript_vtt_upload_imported(self):
        self._add_key()
        extract = self._patch(
            subtitle_extract, "extract_subtitles",
            return_value={"status": "ok", "errors": {}},
        )
        self._patch(translator, "_call_gemini_text", return_value="नमस्ते")
        vtt = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.500 align:start\n"
            "আমার নাম জন\n\n"
            "00:00:04.000 --> 00:00:06.500\n"
            "আমি এখানে থাকি\n"
        )
        res = self._upload(
            transcript=("subs.vtt", vtt.encode("utf-8"), "text/vtt"),
            subtitle_source="user_transcript",
        )
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]
        self._wait_upload_done(job_id)
        extract.assert_not_called()
        raw = json.loads(
            (self.upload_root / job_id / "subtitles_zh_raw.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([s["text"] for s in raw["subtitles"]], ["আমার নাম জন", "আমি এখানে থাকি"])

    def test_gemini_extract_ignores_uploaded_transcript_file(self):
        self._add_key()
        extract = self._patch(
            subtitle_extract,
            "_call_gemini",
            return_value=[
                {"text": "你好", "start_sec": 0.0, "end_sec": 1.5},
            ],
        )
        self._patch(translator, "_call_gemini_text", return_value="नमस्ते")

        res = self._upload(
            transcript=("subs.srt", SRT_SAMPLE.encode("utf-8"), "application/x-subrip"),
            subtitle_source="gemini_extract",
        )
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]
        self._wait_upload_done(job_id)
        job_dir = self.upload_root / job_id

        # F1 must still have run — the attached file is ignored entirely.
        extract.assert_called()
        raw = json.loads(
            (job_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )
        self.assertEqual([s["text"] for s in raw["subtitles"]], ["你好"])
        # No transcript file was persisted for the chain to import.
        self.assertFalse(list(job_dir.glob("transcript_upload.*")))
        cfg = job_config.read_config(job_id)
        self.assertEqual(cfg["subtitle_source"], "gemini_extract")

    def test_user_transcript_without_file_rejected_nothing_saved(self):
        self._add_key()
        res = self._upload(subtitle_source="user_transcript")
        self.assertEqual(res.status_code, 400, res.text)
        self.assertIn("ফাইল আপলোড করা হয়নি", res.json()["detail"])
        self.assertEqual(
            list(self.upload_root.iterdir()) if self.upload_root.exists() else [],
            [],
        )

    def test_invalid_subtitle_source_rejected_nothing_saved(self):
        self._add_key()
        res = self._upload(subtitle_source="bogus_source")
        self.assertEqual(res.status_code, 400, res.text)
        self.assertIn("invalid subtitle source", res.json()["detail"])
        self.assertEqual(
            list(self.upload_root.iterdir()) if self.upload_root.exists() else [],
            [],
        )

    def test_target_lang_plumbed_to_config_and_filenames(self):
        self._add_key()
        self._patch(
            subtitle_extract,
            "_call_gemini",
            return_value=[
                {"text": "你好", "start_sec": 0.0, "end_sec": 1.5},
            ],
        )
        self._patch(translator, "_call_gemini_text", return_value="নমস্কার")

        res = self._upload(target_lang="bn")
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]
        self._wait_upload_done(job_id)
        job_dir = self.upload_root / job_id

        cfg = job_config.read_config(job_id)
        self.assertEqual(cfg["target_lang"], "bn")

        # F12b filename wiring already drives the translated artifacts off the
        # config, so a bn job produces the bn-named files (and no hi ones).
        self.assertTrue((job_dir / "subtitles_bn.json").exists())
        self.assertTrue((job_dir / "subtitles_bn.srt").exists())
        self.assertFalse((job_dir / "subtitles_hi.json").exists())

    def test_invalid_target_lang_rejected_nothing_saved(self):
        self._add_key()
        res = self._upload(target_lang="fr")
        self.assertEqual(res.status_code, 400, res.text)
        self.assertIn("invalid target lang", res.json()["detail"])
        self.assertEqual(
            list(self.upload_root.iterdir()) if self.upload_root.exists() else [],
            [],
        )


if __name__ == "__main__":
    unittest.main()
