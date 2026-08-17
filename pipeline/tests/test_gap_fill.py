"""Tests for F12b Part C: gap-fill for uploaded transcripts.

Covers gap detection, the chronological merge + re-save of re-extracted
windows, the max-windows cap, the non-blocking failure path, and the
HTTP-level wiring that surfaces a Bengali warning in job status while the
rest of the pipeline continues.
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
    key_store,
    render_final,
    subtitle_extract,
    transcript_import,
    translator,
    video_ingest,
    voiceover_auto,
)

# Entry 1 ends at 3.5s; entry 2 starts at 11.0s -> a 7.5s gap, above the
# 6.0s TRANSCRIPT_GAP_FILL_THRESHOLD_SEC.
GAPPED_SRT = """\
1
00:00:01,000 --> 00:00:03,500
আমার নাম জন

2
00:00:11,000 --> 00:00:13,500
আমি এখানে থাকি
"""

TIGHT_SRT = """\
1
00:00:01,000 --> 00:00:03,500
আমার নাম জন

2
00:00:04,000 --> 00:00:06,500
আমি এখানে থাকি
"""

# Two 10-second gaps: [1, 11] and [12, 22].
THREE_GAP_SRT = """\
1
00:00:00,000 --> 00:00:01,000
ক

2
00:00:11,000 --> 00:00:12,000
খ

3
00:00:22,000 --> 00:00:23,000
গ
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


class DetectGapsTest(unittest.TestCase):
    def test_detects_gap_between_entries(self):
        entries = [
            {"text": "a", "start_sec": 0.0, "end_sec": 1.0},
            {"text": "b", "start_sec": 9.0, "end_sec": 10.0},
        ]
        self.assertEqual(
            transcript_import.detect_gaps(entries, threshold_sec=6.0),
            [{"start_sec": 1.0, "end_sec": 9.0}],
        )

    def test_overlapping_entries_never_create_gaps(self):
        entries = [
            {"text": "a", "start_sec": 0.0, "end_sec": 5.0},
            {"text": "b", "start_sec": 4.0, "end_sec": 20.0},
            {"text": "c", "start_sec": 19.0, "end_sec": 21.0},
        ]
        self.assertEqual(
            transcript_import.detect_gaps(entries, threshold_sec=6.0), []
        )

    def test_untimed_entries_are_skipped(self):
        entries = [
            {"text": "no timing"},
            {"text": "a", "start_sec": 0.0, "end_sec": 1.0},
            {"text": "c", "start_sec": 12.0, "end_sec": 13.0},
        ]
        self.assertEqual(
            transcript_import.detect_gaps(entries, threshold_sec=6.0),
            [{"start_sec": 1.0, "end_sec": 12.0}],
        )

    def test_default_threshold_comes_from_config(self):
        from pipeline import config

        self.assertEqual(
            transcript_import.detect_gaps(entries=[
                {"text": "a", "start_sec": 0.0, "end_sec": 1.0},
                {"text": "b", "start_sec": 9.0, "end_sec": 10.0},
            ]),
            [{"start_sec": 1.0, "end_sec": 9.0}],
        )
        self.assertEqual(
            config.TRANSCRIPT_GAP_FILL_THRESHOLD_SEC, 6.0
        )


class FillGapsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-gap"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        self._write_transcript(GAPPED_SRT)

    def _write_transcript(self, content):
        (self.job_dir / "transcript_upload.srt").write_text(
            content, encoding="utf-8"
        )

    def _import(self):
        return transcript_import.import_transcript(
            self.job_id, upload_root=self.upload_root
        )

    def _patched_extract(self, found):
        patch = mock.patch.object(
            subtitle_extract, "extract_window", return_value=found
        )
        patch.start()
        self.addCleanup(patch.stop)
        return subtitle_extract.extract_window

    def _raw_subtitles(self):
        return json.loads(
            (self.job_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )["subtitles"]

    def test_mid_sequence_gap_filled_and_merged(self):
        result = self._import()
        extract = self._patched_extract(
            [{"text": "নিখোঁজ অংশ", "start_sec": 5.0, "end_sec": 6.0}]
        )
        merged, stats = transcript_import.fill_gaps(
            self.job_id, result, upload_root=self.upload_root
        )
        self.assertEqual(stats["detected"], 1)
        self.assertEqual(stats["attempted"], 1)
        self.assertEqual(stats["filled"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["added_entries"], 1)
        self.assertEqual(stats["windows"][0]["outcome"], "filled")
        extract.assert_called_once()
        args = extract.call_args.args
        self.assertEqual(args[0], self.job_id)
        self.assertEqual(args[1], 3.5)
        self.assertEqual(args[2], 11.0)
        # Merged chronologically, in the exact transcript schema.
        self.assertEqual(
            [s["text"] for s in merged["subtitles"]],
            ["আমার নাম জন", "নিখোঁজ অংশ", "আমি এখানে থাকি"],
        )
        for entry in merged["subtitles"]:
            self.assertIn("start_sec", entry)
            self.assertIn("end_sec", entry)
        # Re-saved to disk so B2 consumes the filled transcript.
        self.assertEqual(len(self._raw_subtitles()), 3)

    def test_no_gaps_untouched_and_no_gemini_call(self):
        self._write_transcript(TIGHT_SRT)
        result = self._import()
        extract = self._patched_extract(
            [{"text": "x", "start_sec": 2.0, "end_sec": 3.0}]
        )
        merged, stats = transcript_import.fill_gaps(
            self.job_id, result, upload_root=self.upload_root
        )
        self.assertEqual(stats["detected"], 0)
        self.assertEqual(stats["attempted"], 0)
        self.assertEqual(stats["filled"], 0)
        self.assertEqual(stats["failed"], 0)
        extract.assert_not_called()
        self.assertIs(merged, result)

    def test_failed_window_is_non_blocking(self):
        result = self._import()
        extract = self._patched_extract(None)
        merged, stats = transcript_import.fill_gaps(
            self.job_id, result, upload_root=self.upload_root
        )
        self.assertEqual(stats["detected"], 1)
        self.assertEqual(stats["attempted"], 1)
        self.assertEqual(stats["filled"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["windows"][0]["outcome"], "failed")
        extract.assert_called_once()
        # Nothing added, nothing re-saved: transcript used as-is.
        self.assertIs(merged, result)
        self.assertEqual(len(self._raw_subtitles()), 2)

    def test_max_windows_caps_attempts_largest_first(self):
        self._write_transcript(THREE_GAP_SRT)
        result = self._import()
        extract = self._patched_extract([])
        merged, stats = transcript_import.fill_gaps(
            self.job_id, result, upload_root=self.upload_root, max_windows=1
        )
        self.assertEqual(stats["detected"], 2)
        self.assertEqual(stats["attempted"], 1)
        extract.assert_called_once()
        # Two 10s gaps are tied; only one window is attempted either way.
        self.assertIs(merged, result)


class GapFillHttpTest(unittest.TestCase):
    """F12b /upload wiring: gap-fill runs on the user_transcript path."""

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

    def _upload_transcript(self, content):
        files = {"file": ("sample.mp4", self.video_bytes, "video/mp4")}
        files["transcript"] = (
            "gapped.srt", content.encode("utf-8"), "application/x-subrip",
        )
        data = {
            "voice_source": "user_upload",
            "subtitle_source": "user_transcript",
        }
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

    def test_failed_gap_fill_surfaces_bn_warning_and_pipeline_continues(self):
        self._add_key()
        self._patch(subtitle_extract, "extract_window", return_value=None)
        self._patch(
            translator, "_call_gemini_text",
            return_value="नमस्ते\nनमस्ते\nनमस्ते",
        )
        res = self._upload_transcript(GAPPED_SRT)
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]

        body = self._wait_upload_done(job_id)
        stage = body["stages"]["upload_pipeline"]
        self.assertEqual(stage["state"], "done")
        self.assertIn("gap_fill_warning_bn", stage)
        self.assertEqual(stage["gap_fill_stats"]["detected"], 1)
        self.assertEqual(stage["gap_fill_stats"]["failed"], 1)

        # Pipeline continued past the gap-fill failure down to C1.
        job_dir = self.upload_root / job_id
        for name in ("subtitles_zh.json", "subtitles_hi.json"):
            self.assertTrue((job_dir / name).exists(), f"missing {name}")

    def test_filled_gap_flows_into_downstream_subtitles(self):
        self._add_key()
        self._patch(
            subtitle_extract, "extract_window",
            return_value=[{"text": "নিখোঁজ অংশ", "start_sec": 5.0, "end_sec": 6.0}],
        )
        self._patch(
            translator, "_call_gemini_text",
            return_value="नमस्ते\nनমস্কার\nধন্যবাদ",
        )
        res = self._upload_transcript(GAPPED_SRT)
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]

        body = self._wait_upload_done(job_id)
        stage = body["stages"]["upload_pipeline"]
        self.assertEqual(stage["state"], "done")
        self.assertEqual(stage["gap_fill_stats"]["detected"], 1)
        self.assertEqual(stage["gap_fill_stats"]["filled"], 1)
        self.assertNotIn("gap_fill_warning_bn", stage)

        job_dir = self.upload_root / job_id
        raw = json.loads(
            (job_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [s["text"] for s in raw["subtitles"]],
            ["আমার নাম জন", "নিখোঁজ অংশ", "আমি এখানে থাকি"],
        )
        for name in ("subtitles_zh.json", "subtitles_hi.json"):
            self.assertTrue((job_dir / name).exists(), f"missing {name}")


if __name__ == "__main__":
    unittest.main()
