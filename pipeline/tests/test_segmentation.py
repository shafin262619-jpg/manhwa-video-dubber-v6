"""Tests for pipeline.segmentation (F13b transcript-gap segmentation)."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline import segmentation, video_ingest


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _make_video(path, seconds=1):
    _require_tools()
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c=black:s=320x240:d={seconds}",
            "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono:d={seconds}",
            "-pix_fmt", "yuv420p", "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )


def _subs(pairs):
    """Build a subtitle list from (start_sec, end_sec) pairs with dummy text."""
    return [
        {"text": f"line {i}", "start_sec": start, "end_sec": end}
        for i, (start, end) in enumerate(pairs)
    ]


class SegmentationBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-seg"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def _write_raw(self, subtitles, duration):
        (self.job_dir / "job_meta.json").write_text(
            json.dumps({"job_id": self.job_id, "duration_sec": duration}),
            encoding="utf-8",
        )
        (self.job_dir / "subtitles_zh_raw.json").write_text(
            json.dumps(
                {
                    "job_id": self.job_id,
                    "status": "ok",
                    "chunked": False,
                    "segments_count": 1,
                    "failed_segments": [],
                    "errors": {},
                    "subtitles": subtitles,
                }
            ),
            encoding="utf-8",
        )


class BuildSegmentPlanTest(SegmentationBase):
    def test_short_video_yields_one_segment(self):
        self._write_raw(
            _subs([(0.0, 5.0), (6.0, 10.0), (12.0, 20.0)]), 30.0
        )
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=300)
        self.assertEqual(len(plan["segments"]), 1)
        seg = plan["segments"][0]
        self.assertEqual(seg["start_sec"], 0.0)
        self.assertEqual(seg["end_sec"], 30.0)
        self.assertEqual(seg["entries_count"], 3)

    def test_gap_less_long_video_yields_one_segment(self):
        # Continuous dialogue with no inter-line gaps: cutting would split a
        # spoken line, so the whole video stays one segment.
        subs = []
        t = 0.0
        for i in range(30):
            subs.append({"text": f"line {i}", "start_sec": t, "end_sec": t + 9.0})
            t += 10.0
        self._write_raw(subs, 310.0)
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=300)
        self.assertEqual(len(plan["segments"]), 1)
        self.assertEqual(plan["segments"][0]["entries_count"], 30)

    def test_trailing_tiny_segment_is_folded_into_previous(self):
        # The best cut near the 300s target is at 300s, but cutting there would
        # strand a 100s trailing piece (< 150s), so the whole 400s video must
        # stay one segment.
        self._write_raw(_subs([(0.0, 5.0), (200.0, 205.0), (395.0, 400.0)]), 400.0)
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=300)
        self.assertEqual(len(plan["segments"]), 1)
        self.assertEqual(plan["segments"][0]["end_sec"], 400.0)

    def test_multi_segment_bounds_never_cut_mid_dialogue(self):
        # Gaps between the pairs place midpoints at 15, 65, 125, 225, 330,
        # 380, 425, 475, 560, 635. Segment 1 targets ~300 -> 330 (closest
        # gap); segment 2 would target ~630 -> 635 but that strands a 65s
        # tail, so it extends to 700.
        self._write_raw(
            _subs(
                [
                    (0.0, 10.0), (20.0, 30.0), (100.0, 120.0), (130.0, 150.0),
                    (300.0, 320.0), (340.0, 360.0), (400.0, 420.0),
                    (430.0, 450.0), (500.0, 520.0), (600.0, 620.0),
                    (650.0, 670.0),
                ]
            ),
            700.0,
        )
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=300)
        segments = plan["segments"]
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["start_sec"], 0.0)
        self.assertEqual(segments[0]["end_sec"], 330.0)
        self.assertEqual(segments[1]["start_sec"], 330.0)
        self.assertEqual(segments[1]["end_sec"], 700.0)

        # Every subtitle line belongs to exactly one segment, untouched:
        # entries are only assigned by start_sec < end boundary.
        all_first = segments[0]["first_subtitle_index"]
        self.assertEqual(all_first, 0)
        self.assertEqual(
            segments[1]["first_subtitle_index"],
            segments[0]["last_subtitle_index"] + 1,
        )
        total = sum(s["entries_count"] for s in segments)
        self.assertEqual(total, 11)

    def test_ties_favour_the_later_gap(self):
        # Cut midpoints land at 102.5 and 497.5, both exactly 197.5s away from
        # the 300s target: the later cut wins, yielding [0,497.5] + [497.5,1000].
        # (Favouring 102.5 instead would produce three segments.)
        self._write_raw(
            _subs([(0.0, 5.0), (200.0, 205.0), (790.0, 795.0)]), 1000.0
        )
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=300)
        segments = plan["segments"]
        self.assertEqual(
            [(s["start_sec"], s["end_sec"]) for s in segments],
            [(0.0, 497.5), (497.5, 1000.0)],
        )

    def test_plan_is_persisted_and_loadable(self):
        self._write_raw(_subs([(0.0, 5.0), (6.0, 10.0)]), 20.0)
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=300)
        # A single-segment plan is persisted and loadable but does NOT mark the
        # job as segmented (short videos keep the whole-video flow).
        self.assertFalse(segmentation.is_segmented(self.job_id))
        loaded = segmentation.load_plan(self.job_id)
        self.assertEqual(loaded, plan)
        self.assertEqual(loaded["strategy"], "transcript_gap")
        self.assertEqual(loaded["source_duration_sec"], 20.0)

    def test_is_segmented_true_only_for_multi_segment_plans(self):
        self._write_raw(
            _subs([(0.0, 5.0), (200.0, 205.0), (400.0, 405.0)]), 1000.0
        )
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=300)
        self.assertGreater(len(plan["segments"]), 1)
        self.assertTrue(segmentation.is_segmented(self.job_id))
        self.assertTrue(segmentation.is_segmented(self.job_id, upload_root=self.upload_root))

    def test_missing_raw_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            segmentation.build_segment_plan(self.job_id)


class MaterializeSegmentTest(SegmentationBase):
    def setUp(self):
        super().setUp()
        _require_tools()
        self.video_path = self.job_dir / "source.mp4"
        _make_video(self.video_path, seconds=8)
        self._write_raw(
            _subs(
                [
                    (0.5, 1.5), (2.0, 2.5), (3.5, 4.5), (5.0, 5.5),
                    (6.5, 7.5),
                ]
            ),
            8.0,
        )

    def test_materialize_cuts_video_and_rebases_slice(self):
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=3)
        segments = plan["segments"]
        self.assertEqual(len(segments), 3)

        seg_dir = segmentation.materialize_segment(
            self.job_id, plan, segments[0]
        )
        self.assertEqual(
            seg_dir, segmentation.segment_dir(self.job_id, 0)
        )
        self.assertTrue((seg_dir / "source.mp4").exists())

        meta = json.loads((seg_dir / "job_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["segment_index"], 0)
        self.assertEqual(meta["segment_start_sec"], 0.0)
        self.assertAlmostEqual(meta["duration_sec"], 3.0, delta=0.5)

        raw = json.loads(
            (seg_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )
        self.assertEqual(raw["status"], "ok")
        self.assertFalse(raw["chunked"])
        self.assertEqual(
            [(s["text"], s["start_sec"], s["end_sec"]) for s in raw["subtitles"]],
            [("line 0", 0.5, 1.5), ("line 1", 2.0, 2.5)],
        )

    def test_materialize_rebases_by_segment_offset(self):
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=3)
        seg_dir = segmentation.materialize_segment(self.job_id, plan, plan["segments"][1])
        raw = json.loads(
            (seg_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )
        # Original entries (3.5-4.5) and (5.0-5.5) shifted by -3.0.
        self.assertEqual(
            [(s["text"], s["start_sec"], s["end_sec"]) for s in raw["subtitles"]],
            [("line 2", 0.5, 1.5), ("line 3", 2.0, 2.5)],
        )

    def test_materialize_is_idempotent(self):
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=3)
        first = segmentation.materialize_segment(self.job_id, plan, plan["segments"][0])
        second = segmentation.materialize_segment(self.job_id, plan, plan["segments"][0])
        self.assertEqual(first, second)
        raw = json.loads(
            (first / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(raw["subtitles"]), 2)

    def test_missing_source_raises(self):
        (self.job_dir / "source.mp4").unlink()
        plan = segmentation.build_segment_plan(self.job_id, target_duration_sec=3)
        with self.assertRaises(FileNotFoundError):
            segmentation.materialize_segment(self.job_id, plan, plan["segments"][0])


class SegmentKeyTest(unittest.TestCase):
    def test_segment_key_format(self):
        self.assertEqual(segmentation.segment_key(0), "seg_000")
        self.assertEqual(segmentation.segment_key(12), "seg_012")
