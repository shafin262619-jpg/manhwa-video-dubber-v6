"""Tests for F13b Part C — segmented-job-aware resume (sequential continuation).

The F13b Part B orchestrator runs the whole per-segment chain in one unbroken
call. Part C adds the continuation path: when a segmented job's process is
interrupted (crash mid-segment, manual stop), ``resume.resume_job`` must
detect the job is segmented, derive the resume point from the per-segment job
status, and re-enter the orchestrator at that segment/stage — never re-running
completed segments or stages, and never falling through to the whole-video
resume path (which would corrupt per-segment work).
"""

import contextlib
import json
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from pipeline import (
    auto_cut,
    job_status as store,
    lang_files,
    render_final,
    resume,
    segmented_pipeline,
    segmentation,
    video_ingest,
    voiceover_auto,
    voiceover_unify,
)


def _ok_result(stdout=""):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _wav_bytes(duration_sec):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clip.wav"
        voiceover_auto._make_silence(duration_sec, path)
        return path.read_bytes()


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


class F13bPartCBase(unittest.TestCase):
    def setUp(self):
        _require_tools()
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.output_root = Path(self._tmp) / "outputs"
        self.job_id = "job-seg-c"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        render_final.OUTPUT_ROOT = self.output_root
        self.addCleanup(self._restore_paths)

    def _restore_paths(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        render_final.OUTPUT_ROOT = self._orig_output_root

    def _make_job(self, seconds=12, target=3):
        """A multi-segment job (real plan + per-segment status)."""
        _make_video(self.job_dir / "source.mp4", seconds=seconds)
        (self.job_dir / "job_meta.json").write_text(
            json.dumps({"job_id": self.job_id, "duration_sec": float(seconds)}),
            encoding="utf-8",
        )
        raw = {
            "job_id": self.job_id,
            "status": "ok",
            "chunked": False,
            "segments_count": 1,
            "failed_segments": [],
            "errors": {},
            "subtitles": [
                {"text": f"line {i}", "start_sec": s, "end_sec": s + 0.5}
                for i, s in enumerate([0.5, 2.0, 3.5, 5.0, 6.5, 8.0, 9.5, 11.0])
            ],
        }
        (self.job_dir / "subtitles_zh_raw.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.plan = segmentation.build_segment_plan(
            self.job_id, upload_root=self.upload_root, target_duration_sec=target
        )
        store.init_segments(self.job_id, self.plan)

    def _write_subtitles(self):
        """Top-level whole-video subtitles for a NON-segmented job."""
        (self.job_dir / "subtitles_hi.json").write_text(
            json.dumps(
                [
                    {"serial": 1, "text_zh": "A", "text_hi": "पहला"},
                    {"serial": 2, "text_zh": "B", "text_hi": "दूसरा"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.job_dir / "subtitles_zh.json").write_text(
            json.dumps(
                [
                    {"serial": 1, "text_zh": "A", "start_sec": 0.0, "end_sec": 1.5},
                    {"serial": 2, "text_zh": "B", "start_sec": 2.0, "end_sec": 3.5},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _write_choice(self, mode="auto_tts"):
        (self.job_dir / "voice_source_choice.json").write_text(
            json.dumps({"job_id": self.job_id, "mode": mode}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _mock_auto_run(self, calls):
        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if cmd and cmd[0] == "ffprobe":
                return _ok_result(json.dumps(self._probe_for(cmd[-1])))
            if cmd and cmd[0] == "ffmpeg":
                Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(cmd[-1]).write_bytes(b"out")
            return _ok_result()

        return fake_run

    def _probe_for(self, path):
        name = Path(path).name
        if name == "voiceover_hi.wav":
            return {"format": {"duration": "2.0"}, "streams": []}
        if name == "source.mp4":
            return {
                "format": {"duration": "10.0"},
                "streams": [{"codec_type": "video", "r_frame_rate": "25/1"}],
            }
        if name in ("draft_final_video.mp4", "final_video.mp4"):
            return {
                "format": {"duration": "10.0"},
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            }
        return {"format": {}, "streams": []}


class VoiceoverChainFakes:
    """Real-looking D2/D4/E1/E2/F3 fakes writing the real artifact names."""

    def __init__(self):
        self.calls = []

    def _seg(self, job_dir):
        return Path(job_dir).name

    def d2(self, job_id, upload_root=None, call_budget=None, job_dir=None):
        self.calls.append(("D2", self._seg(job_dir)))
        lang = lang_files.target_lang(job_id, upload_root)
        seg = Path(job_dir)
        (seg / lang_files.timestamps_auto(lang)).write_text(
            json.dumps([{"serial": 1, "start_sec": 0.1, "end_sec": 0.9}])
        )
        return {"status": "ok"}

    def d4(self, job_id, upload_root=None, job_dir=None):
        self.calls.append(("D4", self._seg(job_dir)))
        lang = lang_files.target_lang(job_id, upload_root)
        seg = Path(job_dir)
        (seg / lang_files.timestamps_final(lang)).write_text(
            json.dumps([{"serial": 1, "start_sec": 0.1, "end_sec": 0.9}])
        )
        return {"status": "ok"}

    def e1(self, job_id, upload_root=None, job_dir=None):
        self.calls.append(("E1", self._seg(job_dir)))
        seg = Path(job_dir)
        (seg / "edit_guideline.json").write_text(json.dumps({"mode": "dub"}))
        return {}

    def e2(self, job_id, upload_root=None, job_dir=None, progress_cb=None):
        self.calls.append(("E2", self._seg(job_dir)))
        seg = Path(job_dir)
        (seg / "draft_final_video.mp4").write_bytes(b"draft")
        return {}

    def f3(self, job_id, upload_root=None, output_root=None, job_dir=None,
           output_path=None):
        self.calls.append(("F3", self._seg(job_dir)))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"final")
        return {"final_path": str(output_path)}


class UploadChainFakes:
    """Hermetic B2/whisper/C1 fakes writing the real artifact names.

    Tracks which segment each stage ran against so a test can prove a
    completed segment's upload stages were never re-touched on resume while
    still letting fresh segments run their (fake) upload work.

    ``forbidden`` is a set of ``(stage, seg_key)`` tuples that must never run
    again — the resume may only skip work already completed before the crash.
    A stage whose crash happened *mid-upload* (e.g. C1) is deliberately NOT
    forbidden so the resume legitimately re-runs it.
    """

    def __init__(self, forbidden=None):
        self.forbidden = set(forbidden or ())
        self.seen = {"B2": [], "whisper": [], "C1": []}

    def _guard(self, stage, seg_dir):
        if (stage, seg_dir.name) in self.forbidden:
            raise AssertionError(f"{stage} must not re-run on resume for {seg_dir.name}")

    def b2(self, job_id, upload_root=None, call_budget=None, auto_repair=True,
           job_dir=None, time_offset_sec=0.0):
        seg = Path(job_dir)
        self._guard("B2", seg)
        self.seen["B2"].append(seg.name)
        raw = json.loads((seg / "subtitles_zh_raw.json").read_text(encoding="utf-8"))
        entries = raw.get("subtitles") or []
        (seg / "subtitles_zh.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        (seg / "subtitle_qa.json").write_text("{}", encoding="utf-8")
        return entries

    def whisper(self, job_id, upload_root=None, logger_=None, job_dir=None):
        seg = Path(job_dir)
        self._guard("whisper", seg)
        self.seen["whisper"].append(seg.name)
        (seg / "subtitle_qa_whisper.json").write_text("{}", encoding="utf-8")
        return {}

    def c1(self, job_id, upload_root=None, call_budget=None, max_split_rounds=4,
           job_dir=None):
        seg = Path(job_dir)
        self._guard("C1", seg)
        self.seen["C1"].append(seg.name)
        lang = lang_files.target_lang(job_id, upload_root)
        entries = json.loads((seg / "subtitles_zh.json").read_text(encoding="utf-8"))
        (seg / lang_files.subtitles_json(lang)).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        return entries


class FindSegmentedResumePointTest(F13bPartCBase):
    def test_all_done_returns_none(self):
        self._make_job()
        self.assertGreater(len(self.plan["segments"]), 1)
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        fakes = VoiceoverChainFakes()
        with mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=fakes.d2,
        ), mock.patch.object(
            segmented_pipeline.voiceover_unify, "unify_voiceover_timestamps",
            side_effect=fakes.d4,
        ), mock.patch.object(
            segmented_pipeline.edit_guideline, "build_edit_guideline",
            side_effect=fakes.e1,
        ), mock.patch.object(
            segmented_pipeline.auto_cut, "build_draft_video", side_effect=fakes.e2,
        ), mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            segmented_pipeline.run_segmented_pipeline(
                self.job_id, upload_root=self.upload_root, call_budget=0
            )
        self.assertIsNone(
            resume.find_segmented_resume_point(self.job_id, upload_root=self.upload_root)
        )

    def test_first_incomplete_segment_is_the_resume_point(self):
        self._make_job()
        # Simulate a crash right after segment 0 completed: segment 0 done,
        # everything after still pending.
        seg0 = self.plan["segments"][0]
        seg_dir = segmentation.materialize_segment(
            self.job_id, self.plan, seg0, self.upload_root
        )
        fakes = VoiceoverChainFakes()
        with mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=fakes.d2,
        ), mock.patch.object(
            segmented_pipeline.voiceover_unify, "unify_voiceover_timestamps",
            side_effect=fakes.d4,
        ), mock.patch.object(
            segmented_pipeline.edit_guideline, "build_edit_guideline",
            side_effect=fakes.e1,
        ), mock.patch.object(
            segmented_pipeline.auto_cut, "build_draft_video", side_effect=fakes.e2,
        ), mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            segmented_pipeline.run_segment_upload_chain(
                self.job_id, seg0, seg_dir, self.upload_root, call_budget=0
            )
            final0 = segmented_pipeline.run_segment_voiceover_chain(
                self.job_id, seg0, seg_dir, self.upload_root, call_budget=0
            )
        store.mark_segment_done(self.job_id, 0, final_path=str(final0))

        point = resume.find_segmented_resume_point(
            self.job_id, upload_root=self.upload_root
        )
        self.assertEqual(point, {"segment_index": 1})


class ResumeSegmentedJobTest(F13bPartCBase):
    def _patch_voiceover(self, fakes):
        """Enter mock patches for all five voiceover-chain stages."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=fakes.d2,
        ))
        stack.enter_context(mock.patch.object(
            segmented_pipeline.voiceover_unify, "unify_voiceover_timestamps",
            side_effect=fakes.d4,
        ))
        stack.enter_context(mock.patch.object(
            segmented_pipeline.edit_guideline, "build_edit_guideline",
            side_effect=fakes.e1,
        ))
        stack.enter_context(mock.patch.object(
            segmented_pipeline.auto_cut, "build_draft_video", side_effect=fakes.e2,
        ))
        stack.enter_context(mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ))
        return stack

    def _patch_upload(self, fakes):
        """Enter mock patches for the three per-segment upload-chain stages."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            segmented_pipeline.subtitle_builder, "build_subtitle_list",
            side_effect=fakes.b2,
        ))
        stack.enter_context(mock.patch.object(
            segmented_pipeline.subtitle_verify, "whisper_cross_check",
            side_effect=fakes.whisper,
        ))
        stack.enter_context(mock.patch.object(
            segmented_pipeline.translator, "translate_subtitles",
            side_effect=fakes.c1,
        ))
        return stack

    def test_resume_continues_from_segment_2_skips_segment_1(self):
        self._make_job()
        self.assertGreater(len(self.plan["segments"]), 2)
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        # Segment 0 fully complete; segments 1+ untouched (process died between).
        seg0 = self.plan["segments"][0]
        seg_dir = segmentation.materialize_segment(
            self.job_id, self.plan, seg0, self.upload_root
        )
        fakes = VoiceoverChainFakes()
        with self._patch_voiceover(fakes):
            segmented_pipeline.run_segment_upload_chain(
                self.job_id, seg0, seg_dir, self.upload_root, call_budget=0
            )
            final0 = segmented_pipeline.run_segment_voiceover_chain(
                self.job_id, seg0, seg_dir, self.upload_root, call_budget=0
            )
        store.mark_segment_done(self.job_id, 0, final_path=str(final0))
        self.assertEqual(store.read_segment_status(self.job_id, 1)["state"], "pending")
        fakes.calls.clear()

        ufakes = UploadChainFakes(
            forbidden={
                (stage, segmentation.segment_key(0))
                for stage in ("B2", "whisper", "C1")
            }
        )
        with self._patch_upload(ufakes), self._patch_voiceover(fakes):
            result = resume.resume_job(self.job_id, upload_root=self.upload_root)

        self.assertEqual(result["segmented"], True)
        self.assertEqual(len(result["segments"]), len(self.plan["segments"]))
        # Segment 0 must NOT be re-processed by the resume.
        seg0_calls = [c for c in fakes.calls if c[1] == segmentation.segment_key(0)]
        self.assertEqual(seg0_calls, [], "resume must not re-process completed segment 0")
        self.assertNotIn(segmentation.segment_key(0), ufakes.seen["B2"])
        self.assertNotIn(segmentation.segment_key(0), ufakes.seen["C1"])
        # Fresh segments 1+ DID get their upload + voiceover work.
        for index in range(1, len(self.plan["segments"])):
            key = segmentation.segment_key(index)
            self.assertIn(key, ufakes.seen["B2"])
            self.assertIn(key, ufakes.seen["C1"])
            self.assertTrue([c for c in fakes.calls if c[1] == key],
                            f"resume must process segment {index}")
        # And every segment ends done.
        data = store.read_status(self.job_id)
        self.assertEqual(data["segmented"]["overall_state"], "done")
        for seg in self.plan["segments"]:
            self.assertEqual(
                store.read_segment_status(self.job_id, seg["index"])["state"], "done"
            )

    def test_resume_mid_segment_skips_completed_stage(self):
        # Interrupted mid-way through segment 1: C1 (translation) done but the
        # D2 voiceover chain not yet run. Resume must continue from segment 1's
        # D2 without re-running the completed C1 (or B2 / whisper).
        self._make_job()
        self.assertGreater(len(self.plan["segments"]), 2)
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        lang = lang_files.target_lang(self.job_id, self.upload_root)

        def exploding_d2(job_id, upload_root=None, call_budget=None, job_dir=None):
            if Path(job_dir).name == segmentation.segment_key(1):
                raise RuntimeError("killed during seg_001 D2 voiceover")
            return fakes.d2(job_id, upload_root=upload_root,
                            call_budget=call_budget, job_dir=job_dir)

        fakes = VoiceoverChainFakes()
        with mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=exploding_d2,
        ), mock.patch.object(
            segmented_pipeline.voiceover_unify, "unify_voiceover_timestamps",
            side_effect=fakes.d4,
        ), mock.patch.object(
            segmented_pipeline.edit_guideline, "build_edit_guideline",
            side_effect=fakes.e1,
        ), mock.patch.object(
            segmented_pipeline.auto_cut, "build_draft_video", side_effect=fakes.e2,
        ), mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            with self.assertRaises(RuntimeError):
                segmented_pipeline.run_segmented_pipeline(
                    self.job_id, upload_root=self.upload_root, call_budget=0
                )
        self.assertEqual(store.read_segment_status(self.job_id, 0)["state"], "done")
        self.assertEqual(store.read_segment_status(self.job_id, 1)["state"], "error")
        seg1 = segmentation.segment_dir(self.job_id, 1, self.upload_root)
        self.assertTrue((seg1 / lang_files.subtitles_json(lang)).exists(),
                        "C1 finished for segment 1 before the crash")

        fakes.calls.clear()
        ufakes = UploadChainFakes(
            forbidden={
                (stage, segmentation.segment_key(1))
                for stage in ("B2", "whisper", "C1")
            }
        )
        with self._patch_upload(ufakes), self._patch_voiceover(fakes):
            result = resume.resume_job(self.job_id, upload_root=self.upload_root)

        # Segment 1's upload stages (completed pre-crash) must not be re-run.
        self.assertNotIn(segmentation.segment_key(1), ufakes.seen["B2"])
        self.assertNotIn(segmentation.segment_key(1), ufakes.seen["whisper"])
        self.assertNotIn(segmentation.segment_key(1), ufakes.seen["C1"])
        # But its voiceover chain (crashed at D2) must be re-entered.
        seg1_voice = [c for c in fakes.calls if c[1] == segmentation.segment_key(1)]
        self.assertEqual(seg1_voice[0][0], "D2",
                         "resume must restart segment 1 from the crashed D2 stage")
        self.assertEqual(result["segmented"], True)
        data = store.read_status(self.job_id)
        self.assertEqual(data["segmented"]["overall_state"], "done")
        for seg in self.plan["segments"]:
            self.assertEqual(
                store.read_segment_status(self.job_id, seg["index"])["state"], "done"
            )

    def test_resume_interrupted_partway_reaches_full_completion(self):
        # End-to-end: a multi-segment job killed partway through segment 1's
        # translation, resumed, and confirmed to reach full completion.
        self._make_job()
        self.assertGreater(len(self.plan["segments"]), 2)
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        lang = lang_files.target_lang(self.job_id, self.upload_root)
        real_translate = segmented_pipeline.translator.translate_subtitles

        def exploding_c1(job_id, upload_root=None, call_budget=None,
                         max_split_rounds=4, job_dir=None):
            if job_dir and Path(job_dir).name == segmentation.segment_key(1):
                raise RuntimeError("killed during seg_001 C1 translate")
            return real_translate(
                job_id, upload_root=upload_root, call_budget=call_budget,
                max_split_rounds=max_split_rounds, job_dir=job_dir,
            )

        fakes = VoiceoverChainFakes()
        with mock.patch.object(
            segmented_pipeline.translator, "translate_subtitles",
            side_effect=exploding_c1,
        ), mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=fakes.d2,
        ), mock.patch.object(
            segmented_pipeline.voiceover_unify, "unify_voiceover_timestamps",
            side_effect=fakes.d4,
        ), mock.patch.object(
            segmented_pipeline.edit_guideline, "build_edit_guideline",
            side_effect=fakes.e1,
        ), mock.patch.object(
            segmented_pipeline.auto_cut, "build_draft_video", side_effect=fakes.e2,
        ), mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            with self.assertRaises(RuntimeError):
                segmented_pipeline.run_segmented_pipeline(
                    self.job_id, upload_root=self.upload_root, call_budget=0
                )
        self.assertEqual(store.read_segment_status(self.job_id, 0)["state"], "done")
        self.assertEqual(store.read_segment_status(self.job_id, 1)["state"], "error")
        self.assertFalse(
            (segmentation.segment_dir(self.job_id, 1, self.upload_root)
             / lang_files.subtitles_json(lang)).exists(),
            "C1 did not finish for segment 1 before the crash",
        )

        fakes.calls.clear()
        # B2 + whisper completed for segment 1 before the crash; C1 did not
        # (its artifact is missing), so the resume must legitimately re-run C1.
        ufakes = UploadChainFakes(
            forbidden={
                (stage, segmentation.segment_key(1))
                for stage in ("B2", "whisper")
            }
        )
        with self._patch_upload(ufakes), self._patch_voiceover(fakes):
            result = resume.resume_job(self.job_id, upload_root=self.upload_root)

        self.assertEqual(result["segmented"], True)
        self.assertEqual(len(result["segments"]), len(self.plan["segments"]))
        data = store.read_status(self.job_id)
        self.assertEqual(data["segmented"]["overall_state"], "done")
        self.assertEqual(data["state"], "done")
        for seg in self.plan["segments"]:
            self.assertEqual(
                store.read_segment_status(self.job_id, seg["index"])["state"], "done"
            )
            self.assertTrue(
                segmented_pipeline.segment_final_path(self.job_id, seg["index"]).exists()
            )

    def test_resume_fully_complete_job_raises_bengali(self):
        self._make_job()
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        fakes = VoiceoverChainFakes()
        with self._patch_voiceover(fakes):
            segmented_pipeline.run_segmented_pipeline(
                self.job_id, upload_root=self.upload_root, call_budget=0
            )
        with self.assertRaises(resume.SegmentedResumeError) as ctx:
            resume.resume_job(self.job_id, upload_root=self.upload_root)
        self.assertIn("সম্পূর্ণ", str(ctx.exception))
        self.assertIn("রিজিউম", str(ctx.exception))

    def test_resume_corrupted_status_raises_bengali(self):
        self._make_job()
        # A segmented plan exists, but the per-segment status is unreadable:
        # a segment entry missing its ``state`` makes a safe resume impossible.
        broken = {
            "stage": "segmented_pipeline",
            "state": "error",
            "segmented": {"enabled": True, "total_count": 2, "completed_count": 0},
            "segments": {"seg_000": {"index": 0, "stages": {}}},
        }
        store.status_path(self.job_id, self.upload_root).write_text(
            json.dumps(broken, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(resume.SegmentedResumeError) as ctx:
            resume.resume_job(self.job_id, upload_root=self.upload_root)
        self.assertIn("পড়া যায়নি", str(ctx.exception))

    def test_resume_missing_segments_block_raises_bengali(self):
        self._make_job()
        store.status_path(self.job_id, self.upload_root).write_text(
            json.dumps({"stage": "unknown", "state": "not_started"}),
            encoding="utf-8",
        )
        with self.assertRaises(resume.SegmentedResumeError) as ctx:
            resume.resume_job(self.job_id, upload_root=self.upload_root)
        self.assertIn("পড়া যায়নি", str(ctx.exception))


class NonSegmentedResumeRegressionTest(F13bPartCBase):
    def test_non_segmented_resume_unchanged(self):
        # F12c (Part A) regression: a plain whole-video job whose D2 artifact
        # exists but D4 does not must resume from D4, skipping D2 — identical
        # behavior to before the F13b Part C guard was added.
        _require_tools()
        self._write_subtitles()
        self._write_choice("auto_tts")
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        (self.job_dir / "voiceover_hi.wav").write_bytes(_wav_bytes(2.0))
        (self.job_dir / "timestamps_hi_auto.json").write_text(
            json.dumps(
                [
                    {"serial": 1, "start_sec": 0.0, "end_sec": 1.0},
                    {"serial": 2, "start_sec": 1.0, "end_sec": 2.0},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with mock.patch.object(
            voiceover_auto, "generate_auto_voiceover"
        ) as gen_mock, mock.patch.object(
            auto_cut, "_run", side_effect=self._mock_auto_run([])
        ):
            result = resume.resume_job(self.job_id)

        gen_mock.assert_not_called()
        self.assertIsNone(result["voiceover"])
        self.assertEqual(result["final"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
