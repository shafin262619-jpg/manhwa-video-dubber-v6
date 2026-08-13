"""Tests for pipeline.review (F1 per-clip review UI + clip extract).

All ffmpeg calls are mocked: we assert the page structure (one block per
serial, player URL, flagged highlighting, trim form) and that the on-the-fly
clip extract invokes ffmpeg with the serial's target time range.
"""

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import auto_cut, review, video_ingest


def _ok_result(stdout=""):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


class ReviewBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-f1"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

    def _write_guideline(self, entries):
        (self.job_dir / "edit_guideline.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

    def _write_subtitles(self, entries):
        (self.job_dir / "subtitles_hi.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

    def _entry(self, serial, s, e, ts, te, mult, flagged=False, reason=None):
        return {
            "serial": serial,
            "source_start_sec": s,
            "source_end_sec": e,
            "target_start_sec": ts,
            "target_end_sec": te,
            "pts_multiplier": mult,
            "flagged": flagged,
            "flag_reason": reason,
        }


class ReviewItemsTest(ReviewBase):
    def test_items_join_guideline_and_subtitles(self):
        self._write_guideline(
            [
                self._entry(1, 0.0, 6.0, 0.0, 8.0, 8.0 / 6.0),
                self._entry(2, 6.0, 10.0, 8.0, 14.0, 1.0),
            ]
        )
        self._write_subtitles(
            [
                {"serial": 1, "text_zh": "A", "text_hi": "पहली पंक्ति"},
                {"serial": 2, "text_zh": "B", "text_hi": "दूसरी पंक्ति"},
            ]
        )
        items = review.get_review_items(self.job_id, upload_root=self.upload_root)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["serial"], 1)
        self.assertEqual(items[0]["text_hi"], "पहली पंक्ति")
        self.assertAlmostEqual(items[0]["target_duration_sec"], 8.0, places=3)
        self.assertAlmostEqual(items[0]["source_duration_sec"], 6.0, places=3)
        self.assertAlmostEqual(items[0]["pts_multiplier"], round(8.0 / 6.0, 4), places=4)
        self.assertEqual(items[1]["text_hi"], "दूसरी पंक्ति")

    def test_missing_subtitles_gives_empty_text(self):
        self._write_guideline([self._entry(1, 0.0, 6.0, 0.0, 8.0, 1.0)])
        items = review.get_review_items(self.job_id, upload_root=self.upload_root)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text_hi"], "")

    def test_missing_job_raises(self):
        with self.assertRaises(FileNotFoundError):
            review.get_review_items("nope", upload_root=self.upload_root)

    def test_missing_guideline_raises(self):
        with self.assertRaises(FileNotFoundError):
            review.get_review_items(self.job_id, upload_root=self.upload_root)


class BuildReviewPageTest(ReviewBase):
    def test_page_renders_each_serial_with_player_and_subtitle(self):
        self._write_guideline(
            [
                self._entry(1, 0.0, 6.0, 0.0, 8.0, 8.0 / 6.0),
                self._entry(2, 6.0, 10.0, 8.0, 14.0, 1.0),
            ]
        )
        self._write_subtitles(
            [
                {"serial": 1, "text_hi": "पहली पंक्ति"},
                {"serial": 2, "text_hi": "दूसरी"},
            ]
        )
        page = review.build_review_page(self.job_id, upload_root=self.upload_root)
        self.assertIn(f"<title>Review — job {self.job_id}", page)
        self.assertIn(f'src="/review/{self.job_id}/clip/1"', page)
        self.assertIn(f'src="/review/{self.job_id}/clip/2"', page)
        self.assertIn("पहली पंक्ति", page)
        self.assertIn("Target duration: 8.0s", page)

    def test_flagged_serial_is_highlighted(self):
        self._write_guideline(
            [
                self._entry(1, 0.0, 6.0, 0.0, 8.0, 8.0 / 6.0, True, "extreme_speed_ratio"),
                self._entry(2, 6.0, 10.0, 8.0, 14.0, 1.0),
            ]
        )
        page = review.build_review_page(self.job_id, upload_root=self.upload_root)
        self.assertIn('class="review-box flagged"', page)
        self.assertIn("FLAGGED: extreme_speed_ratio", page)
        self.assertIn("flagged serial(s)", page)
        self.assertIn("#serial-1", page)
        self.assertIn('id="serial-1"', page)

    def test_no_flagged_banner_when_clean(self):
        self._write_guideline([self._entry(1, 0.0, 6.0, 0.0, 8.0, 1.0)])
        page = review.build_review_page(self.job_id, upload_root=self.upload_root)
        self.assertNotIn("flagged serial(s)", page)
        self.assertNotIn("review-box flagged", page)

    def test_trim_form_has_start_end_controls(self):
        self._write_guideline([self._entry(1, 2.5, 6.0, 0.0, 8.0, 1.0)])
        page = review.build_review_page(self.job_id, upload_root=self.upload_root)
        self.assertIn(f'action="/review/{self.job_id}/edit"', page)
        self.assertIn('name="new_source_start"', page)
        self.assertIn('name="new_source_end"', page)
        self.assertIn('value="2.5"', page)
        self.assertIn('value="6.0"', page)


class ExtractClipTest(ReviewBase):
    def _write_draft(self):
        (self.job_dir / "draft_final_video.mp4").write_bytes(b"fake-draft")

    def _fake_run(self, calls):
        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if cmd and cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"fake-draft")
            return _ok_result()

        return fake_run

    def test_extract_uses_target_range_and_returns_clip_path(self):
        self._write_guideline([self._entry(1, 0.0, 6.0, 3.5, 9.25, 1.0)])
        self._write_draft()
        calls = []
        with mock.patch.object(auto_cut, "_run", side_effect=self._fake_run(calls)):
            path = review.extract_clip(self.job_id, 1, upload_root=self.upload_root)

        self.assertEqual(path, self.job_dir / "review_clips" / "serial_00001.mp4")
        self.assertTrue(path.exists())
        self.assertEqual(len(calls), 1)
        clip = calls[0]
        self.assertIn("3.500", clip)
        self.assertIn("9.250", clip)
        self.assertTrue(any(arg.endswith("draft_final_video.mp4") for arg in clip))
        self.assertIn("-movflags", clip)
        self.assertIn("+faststart", clip)

    def test_unknown_serial_raises(self):
        self._write_guideline([self._entry(1, 0.0, 6.0, 0.0, 8.0, 1.0)])
        self._write_draft()
        with self.assertRaises(FileNotFoundError):
            review.extract_clip(self.job_id, 99, upload_root=self.upload_root)

    def test_missing_draft_raises(self):
        self._write_guideline([self._entry(1, 0.0, 6.0, 0.0, 8.0, 1.0)])
        with self.assertRaises(FileNotFoundError):
            review.extract_clip(self.job_id, 1, upload_root=self.upload_root)


class ReviewEndpointTest(ReviewBase):
    def setUp(self):
        super().setUp()
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore)
        self.client = TestClient(app)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def _fake_run(self, calls):
        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if cmd and cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"fake-draft")
            return _ok_result()

        return fake_run

    def test_review_page_endpoint_renders(self):
        self._write_guideline(
            [
                self._entry(1, 0.0, 6.0, 0.0, 8.0, 8.0 / 6.0, True, "extreme_speed_ratio"),
                self._entry(2, 6.0, 10.0, 8.0, 14.0, 1.0),
            ]
        )
        res = self.client.get(f"/review/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn(f'src="/review/{self.job_id}/clip/1"', res.text)
        self.assertIn("FLAGGED", res.text)

    def test_review_page_unknown_job_404(self):
        res = self.client.get("/review/missing-job")
        self.assertEqual(res.status_code, 404)

    def test_clip_endpoint_extracts_and_serves(self):
        self._write_guideline([self._entry(1, 0.0, 6.0, 1.0, 4.0, 1.0)])
        (self.job_dir / "draft_final_video.mp4").write_bytes(b"fake-draft")
        calls = []
        with mock.patch.object(auto_cut, "_run", side_effect=self._fake_run(calls)):
            res = self.client.get(f"/review/{self.job_id}/clip/1")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"fake-draft")
        self.assertEqual(res.headers["content-type"], "video/mp4")
        clip = calls[0]
        self.assertIn("1.000", clip)
        self.assertIn("4.000", clip)

    def test_clip_endpoint_unknown_serial_404(self):
        self._write_guideline([self._entry(1, 0.0, 6.0, 1.0, 4.0, 1.0)])
        (self.job_dir / "draft_final_video.mp4").write_bytes(b"fake-draft")
        res = self.client.get(f"/review/{self.job_id}/clip/99")
        self.assertEqual(res.status_code, 404)


class ApplyClipEditTest(ReviewBase):
    def setUp(self):
        super().setUp()
        self._write_inputs()
        self._write_guideline(
            [
                self._entry(1, 0.0, 6.0, 0.0, 8.0, 8.0 / 6.0),
                self._entry(2, 6.0, 10.0, 8.0, 14.0, 1.0),
            ]
        )
        self._write_clips(2)

    def _write_inputs(self):
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        (self.job_dir / "voiceover_hi.wav").write_bytes(b"fake-audio")
        (self.job_dir / "draft_final_video.mp4").write_bytes(b"fake-draft")

    def _write_clips(self, count):
        clips_dir = self.job_dir / auto_cut.DRAFT_CLIPS_DIR
        clips_dir.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (clips_dir / f"serial_{i:05d}.mp4").write_bytes(b"clip")

    def _probe_for(self, path):
        name = Path(path).name
        if name == "voiceover_hi.wav":
            return {"format": {"duration": "14.0"}, "streams": []}
        if name == "source.mp4":
            return {
                "format": {"duration": "60.0"},
                "streams": [{"codec_type": "video", "r_frame_rate": "25/1"}],
            }
        if name == "draft_final_video.mp4":
            return {
                "format": {"duration": "14.0"},
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            }
        return {"format": {}, "streams": []}

    def _run_edit(self, *args, **kwargs):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if cmd and cmd[0] == "ffprobe":
                return _ok_result(json.dumps(self._probe_for(cmd[-1])))
            if cmd and cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"out")
            return _ok_result()

        with mock.patch.object(auto_cut, "_run", side_effect=fake_run):
            result = review.apply_clip_edit(
                self.job_id, *args, upload_root=self.upload_root, **kwargs
            )
        return result, calls

    def _guideline(self):
        return json.loads(
            (self.job_dir / "edit_guideline.json").read_text(encoding="utf-8")
        )

    def test_edit_updates_only_that_serial_entry(self):
        result, _ = self._run_edit(1, new_source_start=1.0, new_source_end=5.0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["serial"], 1)
        self.assertAlmostEqual(result["source_start_sec"], 1.0, places=3)
        self.assertAlmostEqual(result["source_end_sec"], 5.0, places=3)
        # target_duration stays 8.0; new source duration 4.0 -> pts 2.0
        self.assertAlmostEqual(result["target_duration_sec"], 8.0, places=3)
        self.assertAlmostEqual(result["pts_multiplier"], 2.0, places=4)
        self.assertFalse(result["flagged"])
        self.assertIsNone(result["flag_reason"])

        guideline = self._guideline()
        edited = next(e for e in guideline if e["serial"] == 1)
        self.assertAlmostEqual(edited["source_start_sec"], 1.0, places=3)
        self.assertAlmostEqual(edited["source_end_sec"], 5.0, places=3)
        self.assertAlmostEqual(edited["pts_multiplier"], 2.0, places=4)
        # target timing is preserved
        self.assertAlmostEqual(edited["target_start_sec"], 0.0, places=3)
        self.assertAlmostEqual(edited["target_end_sec"], 8.0, places=3)
        # the other serial is untouched
        other = next(e for e in guideline if e["serial"] == 2)
        self.assertAlmostEqual(other["source_start_sec"], 6.0, places=3)
        self.assertAlmostEqual(other["source_end_sec"], 10.0, places=3)
        self.assertAlmostEqual(other["pts_multiplier"], 1.0, places=4)
        self.assertFalse(other["flagged"])

    def test_edit_recomputes_extreme_speed_flag(self):
        # source [1,2] -> duration 1.0s against target 8.0s -> pts 8.0
        result, _ = self._run_edit(1, new_source_start=1.0, new_source_end=2.0)
        self.assertTrue(result["flagged"])
        self.assertEqual(result["flag_reason"], "extreme_speed_ratio")
        edited = next(e for e in self._guideline() if e["serial"] == 1)
        self.assertTrue(edited["flagged"])
        self.assertEqual(edited["flag_reason"], "extreme_speed_ratio")

    def test_edit_only_start_keeps_end(self):
        result, _ = self._run_edit(1, new_source_start=1.0)
        self.assertAlmostEqual(result["source_start_sec"], 1.0, places=3)
        self.assertAlmostEqual(result["source_end_sec"], 6.0, places=3)
        self.assertAlmostEqual(result["pts_multiplier"], round(8.0 / 5.0, 4), places=4)

    def test_re_cut_uses_new_range_on_guideline_index_clip(self):
        result, calls = self._run_edit(1, new_source_start=1.0, new_source_end=5.0)
        ffmpeg_cmds = [c for c in calls if c[0] == "ffmpeg"]
        # re-cut + concat + mux
        self.assertEqual(len(ffmpeg_cmds), 3)
        recut = ffmpeg_cmds[0]
        self.assertIn("1.000", recut)
        self.assertIn("5.000", recut)
        self.assertTrue(any(arg.endswith("serial_00000.mp4") for arg in recut))
        self.assertTrue(any(arg == "setpts=2.000000*PTS" for arg in recut))
        self.assertTrue(any(arg.endswith("source.mp4") for arg in recut))
        concat = ffmpeg_cmds[1]
        self.assertTrue(any(arg.endswith("concat_video.mp4") for arg in concat))
        mux = ffmpeg_cmds[2]
        self.assertTrue(any(arg.endswith("draft_final_video.mp4") for arg in mux))
        self.assertTrue(any(arg.endswith("voiceover_hi.wav") for arg in mux))
        self.assertEqual(result["re_cut_clip"], str(self.job_dir / "auto_cut_clips" / "serial_00000.mp4"))
        self.assertEqual(result["draft_path"], str(self.job_dir / "draft_final_video.mp4"))

    def test_edit_second_serial_uses_its_index_clip(self):
        # edit serial 2 -> re-cut must target serial_00001.mp4
        result, calls = self._run_edit(2, new_source_start=6.5, new_source_end=9.0)
        ffmpeg_cmds = [c for c in calls if c[0] == "ffmpeg"]
        recut = ffmpeg_cmds[0]
        self.assertTrue(any(arg.endswith("serial_00001.mp4") for arg in recut))
        self.assertIn("6.500", recut)
        self.assertIn("9.000", recut)
        edited = next(e for e in self._guideline() if e["serial"] == 2)
        self.assertAlmostEqual(edited["source_start_sec"], 6.5, places=3)
        self.assertAlmostEqual(edited["source_end_sec"], 9.0, places=3)

    def test_no_edit_given_raises(self):
        with self.assertRaises(ValueError):
            self._run_edit(1)

    def test_invalid_range_raises(self):
        with self.assertRaises(ValueError):
            self._run_edit(1, new_source_start=5.0, new_source_end=5.0)
        with self.assertRaises(ValueError):
            self._run_edit(1, new_source_start=7.0, new_source_end=5.0)
        with self.assertRaises(ValueError):
            self._run_edit(1, new_source_start=-1.0, new_source_end=5.0)

    def test_unknown_serial_raises(self):
        with self.assertRaises(FileNotFoundError):
            self._run_edit(99, new_source_start=1.0, new_source_end=5.0)

    def test_missing_clip_raises(self):
        # remove serial 2's clip -> concat splice cannot run
        (self.job_dir / auto_cut.DRAFT_CLIPS_DIR / "serial_00001.mp4").unlink()
        with self.assertRaises(FileNotFoundError):
            self._run_edit(1, new_source_start=1.0, new_source_end=5.0)

    def test_validation_failure_raises_draft_validation_error(self):
        original_probe = self._probe_for

        def probe_override(path):
            if Path(path).name == "draft_final_video.mp4":
                return {
                    "format": {"duration": "30.0"},
                    "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                }
            return original_probe(path)

        def fake_run(cmd, timeout=None):
            if cmd and cmd[0] == "ffprobe":
                return _ok_result(json.dumps(probe_override(cmd[-1])))
            return _ok_result()

        with mock.patch.object(auto_cut, "_run", side_effect=fake_run):
            with self.assertRaises(review.DraftValidationError):
                review.apply_clip_edit(
                    self.job_id, 1, new_source_start=1.0, new_source_end=5.0,
                    upload_root=self.upload_root,
                )


class ApplyClipEditEndpointTest(ReviewBase):
    def setUp(self):
        super().setUp()
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        (self.job_dir / "voiceover_hi.wav").write_bytes(b"fake-audio")
        (self.job_dir / "draft_final_video.mp4").write_bytes(b"fake-draft")
        clips_dir = self.job_dir / auto_cut.DRAFT_CLIPS_DIR
        clips_dir.mkdir(parents=True)
        for i in range(2):
            (clips_dir / f"serial_{i:05d}.mp4").write_bytes(b"clip")
        self._write_guideline(
            [
                self._entry(1, 0.0, 6.0, 0.0, 8.0, 8.0 / 6.0),
                self._entry(2, 6.0, 10.0, 8.0, 14.0, 1.0),
            ]
        )
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore)
        self.client = TestClient(app)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def _fake_run(self, calls):
        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if cmd and cmd[0] == "ffprobe":
                name = Path(cmd[-1]).name
                if name == "voiceover_hi.wav":
                    duration = "14.0"
                elif name == "source.mp4":
                    return _ok_result(json.dumps({
                        "format": {"duration": "60.0"},
                        "streams": [{"codec_type": "video", "r_frame_rate": "25/1"}],
                    }))
                else:
                    duration = "14.0"
                return _ok_result(json.dumps({
                    "format": {"duration": duration},
                    "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                }))
            if cmd and cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"out")
            return _ok_result()

        return fake_run

    def _guideline(self):
        return json.loads(
            (self.job_dir / "edit_guideline.json").read_text(encoding="utf-8")
        )

    def test_edit_endpoint_applies_and_links_back_to_review(self):
        calls = []
        with mock.patch.object(auto_cut, "_run", side_effect=self._fake_run(calls)):
            res = self.client.post(
                f"/review/{self.job_id}/edit",
                data={"serial": "1", "new_source_start": "1.0", "new_source_end": "5.0"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Edit applied", res.text)
        self.assertIn(f'href="/review/{self.job_id}"', res.text)
        edited = next(e for e in self._guideline() if e["serial"] == 1)
        self.assertAlmostEqual(edited["source_start_sec"], 1.0, places=3)
        self.assertAlmostEqual(edited["source_end_sec"], 5.0, places=3)
        ffmpeg_cmds = [c for c in calls if c[0] == "ffmpeg"]
        self.assertEqual(len(ffmpeg_cmds), 3)

    def test_edit_endpoint_unknown_serial_404(self):
        res = self.client.post(
            f"/review/{self.job_id}/edit",
            data={"serial": "99", "new_source_start": "1.0", "new_source_end": "5.0"},
        )
        self.assertEqual(res.status_code, 404)

    def test_edit_endpoint_invalid_range_400(self):
        res = self.client.post(
            f"/review/{self.job_id}/edit",
            data={"serial": "1", "new_source_start": "5.0", "new_source_end": "5.0"},
        )
        self.assertEqual(res.status_code, 400)

    def test_edit_endpoint_unknown_job_404(self):
        res = self.client.post(
            "/review/missing-job/edit",
            data={"serial": "1", "new_source_start": "1.0", "new_source_end": "5.0"},
        )
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
