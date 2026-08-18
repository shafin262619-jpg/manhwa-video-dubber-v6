"""F14c Part 2 tests: final review page, Confirm button, re-loop wiring.

Covers the page rendered once a segmented job reaches ``final_ready``: the
full assembled video on top, the per-segment review breakdown (reusing the
F14a/F14b cards) below it so an issue can still be reported per segment, and
the "চূর্তিম নিশ্চিতকরণ" confirm button. Confirm records the terminal
``confirmed`` state and hides the issue/confirm controls; reporting an issue
from this page reverts the job to ``in_review`` (showing the segment's
"ঠিক করা হচ্ছে" state) and Part 1's automatic re-trigger brings back an
updated final video once resolved; an assembly failure renders a Bengali
error + retry affordance instead of a broken page; and the History tab links
to this page for jobs in final_ready / confirmed / assembly_failed state.
"""

import html as html_lib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app as app_module
from app import app
from pipeline import (
    history_store,
    job_status as store,
    render_final,
    segmented_pipeline,
    segmentation,
    video_ingest,
    voiceover_unify,
)


def _require_tools():
    if not (subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0
            and subprocess.run(["which", "ffprobe"], capture_output=True).returncode == 0):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _make_video(path, seconds=1):
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


def _probe_duration(path):
    return video_ingest.probe_video(path)["duration_sec"]


class F14cPart2Base(unittest.TestCase):
    def setUp(self):
        _require_tools()
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.output_root = Path(self._tmp) / "outputs"
        self.job_id = "job-f14c2"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        render_final.OUTPUT_ROOT = self.output_root

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        render_final.OUTPUT_ROOT = self._orig_output_root

    def _make_job(self, n_segments=2):
        plan = {
            "job_id": self.job_id,
            "strategy": "transcript_gap",
            "target_duration_sec": 300,
            "source_duration_sec": 700,
            "segments": [
                {
                    "index": i,
                    "start_sec": i * 10.0,
                    "end_sec": (i + 1) * 10.0,
                    "duration_sec": 10.0,
                    "entries_count": 5,
                }
                for i in range(n_segments)
            ],
        }
        plan_path = segmentation.plan_path(self.job_id, upload_root=self.upload_root)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        store.init_segments(self.job_id, plan, upload_root=self.upload_root)
        # /upload/{job_id} only reaches the segmented result/final-review pages
        # when the job's voice source is auto_tts (the whole-video chain must
        # not run for segmented jobs).
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        return plan

    def _segment_video(self, seg_index, seconds):
        path = segmented_pipeline.segment_final_path(
            self.job_id, seg_index, output_root=self.output_root
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        _make_video(path, seconds=seconds)
        return path

    def _mark_done_with_video(self, seg_index, seconds=1):
        path = self._segment_video(seg_index, seconds)
        store.mark_segment_done(
            self.job_id, seg_index, final_path=str(path),
            upload_root=self.upload_root,
        )
        return path

    def _mark_done_no_video(self, seg_index):
        missing = self.output_root / "missing" / f"seg_{seg_index:03d}.mp4"
        store.mark_segment_done(
            self.job_id, seg_index, final_path=str(missing),
            upload_root=self.upload_root,
        )

    def _clean_review(self, seg_index):
        round_no = store.next_review_round(
            self.job_id, seg_index, upload_root=self.upload_root
        )
        store.record_segment_review(
            self.job_id, seg_index, round_no=round_no, issues=[],
            upload_root=self.upload_root,
        )

    def _finalize(self, n_segments=2, seconds=(1, 2)):
        """Every segment done + clean-reviewed, then assemble -> final_ready."""
        self._make_job(n_segments)
        for index, secs in zip(range(n_segments), seconds):
            self._mark_done_with_video(index, seconds=secs)
            self._clean_review(index)
        result = segmented_pipeline.maybe_assemble_final_video(
            self.job_id, self.upload_root
        )
        self.assertIsNotNone(result)
        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_FINAL_READY
        )
        return data


class FinalReviewPageRenderTest(F14cPart2Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self._finalize(seconds=(1, 2))

    def test_final_ready_renders_full_video_segments_and_confirm(self):
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn(f'src="/download/{self.job_id}"', html)
        self.assertIn("চূর্তিম নিশ্চিতকরণ", html)
        self.assertIn("Download final video", html)
        self.assertIn("seg_000", html)
        self.assertIn("seg_001", html)
        self.assertIn(f"/segment-review/{self.job_id}/0", html)
        self.assertIn(f"/segment-review/{self.job_id}/1", html)

    def test_final_ready_shows_done_reviewing_summary_and_version(self):
        res = self.client.get(f"/upload/{self.job_id}")
        html = res.text
        self.assertIn("রিভিউ সম্পন্ন", html)
        self.assertIn("সংস্করণ 1", html)

    def test_in_review_job_still_renders_segment_review_page(self):
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review(0)
        self._mark_done_with_video(1, seconds=1)
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertNotIn("চূর্তিম নিশ্চিতকরণ", html)
        self.assertNotIn(f'src="/download/{self.job_id}"', html)
        self.assertIn(f"/segment-review/{self.job_id}/1", html)


class ConfirmFinalTest(F14cPart2Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self._finalize(seconds=(1, 2))

    def test_confirm_records_terminal_state(self):
        res = self.client.post(
            f"/jobs/{self.job_id}/final-confirm", follow_redirects=False
        )
        self.assertEqual(res.status_code, 303)
        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_CONFIRMED
        )
        confirmation = data["segmented"]["final_confirmation"]
        self.assertTrue(confirmation["user_confirmed"])
        self.assertTrue(confirmation["confirmed_at"])

    def test_confirmed_page_shows_confirmation_and_hides_controls(self):
        self.client.post(
            f"/jobs/{self.job_id}/final-confirm", follow_redirects=False
        )
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn("ভিডিওটি চূর্তিমভাবে নিশ্চিত হয়েছে", html)
        self.assertIn(f'src="/download/{self.job_id}"', html)
        self.assertIn("Download final video", html)
        self.assertNotIn("চূর্তিম নিশ্চিতকরণ", html)
        self.assertNotIn(f"/segment-review/{self.job_id}/0", html)
        self.assertNotIn(f"/segment-review/{self.job_id}/1", html)

    def test_confirm_is_idempotent(self):
        self.client.post(
            f"/jobs/{self.job_id}/final-confirm", follow_redirects=False
        )
        res = self.client.post(
            f"/jobs/{self.job_id}/final-confirm", follow_redirects=False
        )
        self.assertEqual(res.status_code, 303)
        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_CONFIRMED
        )

    def test_confirm_rejected_when_not_final_ready(self):
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review(0)
        self._mark_done_with_video(1, seconds=1)
        res = self.client.post(
            f"/jobs/{self.job_id}/final-confirm", follow_redirects=False
        )
        self.assertEqual(res.status_code, 409)

    def test_confirm_rejected_when_assembly_failed(self):
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review(0)
        self._mark_done_no_video(1)
        self._clean_review(1)
        segmented_pipeline.maybe_assemble_final_video(self.job_id, self.upload_root)
        res = self.client.post(
            f"/jobs/{self.job_id}/final-confirm", follow_redirects=False
        )
        self.assertEqual(res.status_code, 409)


class IssueFromFinalReviewTest(F14cPart2Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self._finalize(seconds=(1, 2))

    def test_issue_from_final_page_reverts_and_shows_being_fixed(self):
        with mock.patch.object(app_module, "_run_segment_rerun"):
            res = self.client.post(
                f"/segment-review/{self.job_id}/1",
                data={"verdict": "issues", "issues": ["audio_glitch"]},
                follow_redirects=False,
            )
        self.assertEqual(res.status_code, 303)
        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_IN_REVIEW
        )
        self.assertEqual(
            data["segmented"]["final_assembly"]["state"],
            store.SEGMENT_ASSEMBLY_STALE,
        )
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertNotIn("চূর্তিম নিশ্চিতকরণ", html)
        self.assertIn("ঠিক করা হচ্ছে", html)

    def test_reassembly_after_fix_returns_new_final_video(self):
        with mock.patch.object(app_module, "_run_segment_rerun"):
            res = self.client.post(
                f"/segment-review/{self.job_id}/1",
                data={"verdict": "issues", "issues": ["audio_glitch"]},
                follow_redirects=False,
            )
        self.assertEqual(res.status_code, 303)
        store.record_segment_rerun(
            self.job_id, 1, triggered_by_round=2, issues=["audio_glitch"],
            target_stage="E2_draft", status=store.SEGMENT_RERUN_OK,
            upload_root=self.upload_root,
        )
        self._mark_done_with_video(1, seconds=3)
        res = self.client.post(
            f"/segment-review/{self.job_id}/1",
            data={"verdict": "clean"},
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 303)

        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_FINAL_READY
        )
        assembly = data["segmented"]["final_assembly"]
        self.assertEqual(assembly["state"], store.SEGMENT_ASSEMBLY_READY)
        self.assertEqual(assembly["version"], 2)
        self.assertEqual(assembly["segment_rounds"], {"seg_000": 1, "seg_001": 4})
        final = Path(assembly["final_path"])
        self.assertTrue(final.exists())
        duration = _probe_duration(final)
        self.assertLessEqual(abs(duration - 4.0), 0.2)

        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn("চূর্তিম নিশ্চিতকরণ", html)
        self.assertIn("সংস্করণ 2", html)
        self.assertIn(f'src="/download/{self.job_id}"', html)


class AssemblyFailureRenderTest(F14cPart2Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review(0)
        self._mark_done_no_video(1)
        self._clean_review(1)
        result = segmented_pipeline.maybe_assemble_final_video(
            self.job_id, self.upload_root
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "failed")

    def test_assembly_failed_page_renders_error_and_retry(self):
        data = store.read_status(self.job_id, self.upload_root)
        error_bn = data["segmented"]["final_assembly"]["error_bn"]
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn("চূর্তিম ভিডিও একত্রকরণ ব্যর্থ হয়েছে", html)
        self.assertIn(html_lib.escape(error_bn), html)
        self.assertIn("আবার চেষ্টা করুন", html)
        self.assertIn(f'action="/jobs/{self.job_id}/final-assembly/retry"', html)
        self.assertNotIn("চূর্তিম নিশ্চিতকরণ", html)

    def test_retry_after_failure_reassembles(self):
        self._mark_done_with_video(1, seconds=2)
        res = self.client.post(
            f"/jobs/{self.job_id}/final-assembly/retry", follow_redirects=False
        )
        self.assertEqual(res.status_code, 303)
        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_FINAL_READY
        )
        assembly = data["segmented"]["final_assembly"]
        self.assertEqual(assembly["state"], store.SEGMENT_ASSEMBLY_READY)
        self.assertTrue(Path(assembly["final_path"]).exists())
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("চূর্তিম নিশ্চিতকরণ", res.text)
        self.assertIn(f'src="/download/{self.job_id}"', res.text)

    def test_retry_rejected_when_not_failed(self):
        self._finalize(seconds=(1, 2))
        res = self.client.post(
            f"/jobs/{self.job_id}/final-assembly/retry", follow_redirects=False
        )
        self.assertEqual(res.status_code, 409)


class HistoryReachabilityTest(F14cPart2Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)

    def _register(self):
        history_store.register_job(
            self.job_id, meta={"target_video_name": "sample.mp4"},
            upload_root=self.upload_root,
        )

    def test_history_surfaces_final_ready_job_and_links_to_final_page(self):
        self._finalize(seconds=(1, 2))
        self._register()
        res = self.client.get("/history")
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn(self.job_id, html)
        self.assertIn(f'href="/upload/{self.job_id}"', html)
        self.assertNotIn(f'href="/review/{self.job_id}"', html)
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("চূর্তিম নিশ্চিতকরণ", res.text)

    def test_history_links_confirmed_job_to_final_page(self):
        self._finalize(seconds=(1, 2))
        self.client.post(
            f"/jobs/{self.job_id}/final-confirm", follow_redirects=False
        )
        self._register()
        res = self.client.get("/history")
        self.assertEqual(res.status_code, 200)
        self.assertIn(f'href="/upload/{self.job_id}"', res.text)
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("ভিডিওটি চূর্তিমভাবে নিশ্চিত হয়েছে", res.text)

    def test_history_links_assembly_failed_job_to_final_page(self):
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review(0)
        self._mark_done_no_video(1)
        self._clean_review(1)
        segmented_pipeline.maybe_assemble_final_video(self.job_id, self.upload_root)
        self._register()
        res = self.client.get("/history")
        self.assertEqual(res.status_code, 200)
        self.assertIn(f'href="/upload/{self.job_id}"', res.text)
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("চূর্তিম ভিডিও একত্রকরণ ব্যর্থ হয়েছে", res.text)

    def test_non_segmented_done_job_keeps_review_link(self):
        plain_job = "job-plain"
        html = app_module._history_card({
            "job_id": plain_job,
            "state": "done",
            "stage": "done",
            "created_at": "2026-01-01T00:00:00",
            "target_video_name": "x.mp4",
            "target_lang": "hi",
            "voice_source": "auto_tts",
        })
        self.assertIn(f'href="/review/{plain_job}"', html)
        self.assertNotIn(f'href="/upload/{plain_job}"', html)


if __name__ == "__main__":
    unittest.main()
