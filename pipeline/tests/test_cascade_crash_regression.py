"""E7 regression: the real-media QA cascade-crash job.

A long subtitle entry ending at 100s followed by 27 entries whose raw starts
(60..86s) all collide with it used to be clamped one-by-one to 100.0s, leaving
zero-duration segments that made ffmpeg abort with "-to value smaller than -ss"
and crashed the whole job (job 97a9b90e-71f4-4d64-931d-b1b5cd194ce2).

This test drives the same pattern through the whole chain
subtitle_builder -> edit_guideline -> auto_cut (ffmpeg mocked) and asserts:

  (ka)  subtitle_builder output has no zero/negative-duration entries,
  (kha) auto_cut never throws even with the degenerate guideline input, and
  (ga)  the job completes with status "ok" instead of failing.
"""

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from pipeline import auto_cut, edit_guideline, subtitle_builder


def _disordered_raw_subs():
    subs = [
        {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
        {"text": "b", "start_sec": 3.0, "end_sec": 100.0},
    ]
    for k in range(27):
        s = 60.0 + k
        subs.append({"text": f"line-{k}", "start_sec": s, "end_sec": s + 1.0})
    return subs


class CascadeCrashRegressionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-e7"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        (self.job_dir / "job_meta.json").write_text(
            json.dumps({"job_id": self.job_id, "duration_sec": 130.0}),
            encoding="utf-8",
        )
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        (self.job_dir / "voiceover_hi.wav").write_bytes(b"fake-audio")

    def _write_raw(self):
        (self.job_dir / "subtitles_zh_raw.json").write_text(
            json.dumps(
                {
                    "job_id": self.job_id,
                    "status": "ok",
                    "chunked": False,
                    "segments_count": 1,
                    "failed_segments": [],
                    "subtitles": _disordered_raw_subs(),
                }
            ),
            encoding="utf-8",
        )

    def _write_hi(self, serialized):
        (self.job_dir / "timestamps_hi_final.json").write_text(
            json.dumps(
                [
                    {
                        "serial": e["serial"],
                        "start_sec": e["start_sec"],
                        "end_sec": e["end_sec"],
                    }
                    for e in serialized
                ]
            ),
            encoding="utf-8",
        )

    def _probe_for(self, path):
        name = Path(path).name
        if name == "voiceover_hi.wav":
            return {"format": {"duration": "120.0"}, "streams": []}
        if name == "source.mp4":
            return {
                "format": {"duration": "130.0"},
                "streams": [{"codec_type": "video", "r_frame_rate": "25/1"}],
            }
        if name == "draft_final_video.mp4":
            return {
                "format": {"duration": "130.0"},
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            }
        return {"format": {}, "streams": []}

    def test_disordered_timestamps_do_not_crash_pipeline(self):
        self._write_raw()

        # (ka) no zero/negative-duration entry in subtitle_builder output.
        serialized = subtitle_builder.build_subtitle_list(
            self.job_id, upload_root=self.upload_root, auto_repair=False
        )
        self.assertEqual(len(serialized), 29)
        for e in serialized:
            self.assertGreater(
                e["end_sec"], e["start_sec"],
                f"zero/negative-duration entry: {e}",
            )

        qa = json.loads(
            (self.job_dir / "subtitle_qa.json").read_text(encoding="utf-8")
        )
        self.assertEqual(qa["duplicate_clusters"], [])
        self.assertTrue(
            any(c["reason"] == "collision_cluster" for c in qa["collision_clusters"])
        )

        # Guideline derives only healthy source windows from the fixed timing.
        self._write_hi(serialized)
        guideline = edit_guideline.build_edit_guideline(
            self.job_id, upload_root=self.upload_root
        )
        self.assertEqual(guideline["flagged_count"], 0)
        for entry in guideline["guideline"]:
            self.assertGreater(
                entry["source_end_sec"], entry["source_start_sec"]
            )

        # (kha + ga) auto_cut completes without raising and renders the draft.
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if cmd and cmd[0] == "ffprobe":
                data = self._probe_for(cmd[-1])
                return types.SimpleNamespace(
                    stdout=json.dumps(data), stderr="", returncode=0
                )
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        with mock.patch.object(auto_cut, "_run", side_effect=fake_run):
            result = auto_cut.build_draft_video(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["clip_count"], 29)
        # Every clip command uses a strictly positive [start, end) window, so
        # the "-to value smaller than -ss" crash cannot occur.
        for cmd in calls:
            if cmd[0] == "ffmpeg" and "-to" in cmd:
                ss = float(cmd[cmd.index("-ss") + 1])
                to = float(cmd[cmd.index("-to") + 1])
                self.assertGreater(to, ss)


if __name__ == "__main__":
    unittest.main()
