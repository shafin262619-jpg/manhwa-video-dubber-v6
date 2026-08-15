"""Tests for pipeline.subtitle_extract (mocked Gemini responses)."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from google.genai import errors as genai_errors
from google.genai import types as genai_types

from pipeline import config, key_store, subtitle_extract, video_ingest
from pipeline.gemini_rotation import CallBudget


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _make_video(path, seconds=1):
    _require_tools()
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c=black:s=320x240:d={seconds}",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
    )


class SubtitleExtractBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.store_path = Path(self._tmp) / "gemini_keys_store.json"
        self.job_id = "job-test"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        (self.job_dir / "source.mp4").write_bytes(b"dummy-video-bytes")
        self._orig_key_path = key_store.KEY_STORE_PATH
        key_store.KEY_STORE_PATH = self.store_path
        subtitle_extract._UPLOAD_CACHE.clear()
        self.addCleanup(self._restore_key_path)

    def _restore_key_path(self):
        key_store.KEY_STORE_PATH = self._orig_key_path

    def _write_meta(self, duration):
        (self.job_dir / "job_meta.json").write_text(
            json.dumps({"job_id": self.job_id, "duration_sec": duration}),
            encoding="utf-8",
        )

    def _set_keys(self, keys):
        self.store_path.write_text(
            json.dumps({"keys": [{"id": f"k{i+1}", "key": k, "label": None}
                                  for i, k in enumerate(keys)]}),
            encoding="utf-8",
        )

    def _read_result(self):
        return json.loads(
            (self.job_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )


class SubtitleExtractSuccessTest(SubtitleExtractBase):
    def test_small_video_happy_path(self):
        self._write_meta(30.0)
        self._set_keys(["key-one"])
        with mock.patch.object(
            subtitle_extract, "_call_gemini", return_value=[
                {"text": "你好", "start_sec": 0.2, "end_sec": 2.1},
                {"text": "世界", "start_sec": 2.3, "end_sec": 4.0},
            ]
        ) as fake:
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["chunked"])
        self.assertEqual(result["segments_count"], 1)
        self.assertEqual(result["failed_segments"], [])
        self.assertEqual(len(result["subtitles"]), 2)
        self.assertEqual(result["subtitles"][0]["text"], "你好")
        fake.assert_called_once()
        self.assertEqual(fake.call_args.args[3], 0.0)
        self.assertTrue((self.job_dir / "subtitles_zh_raw.json").exists())
        self.assertEqual(self._read_result()["status"], "ok")


class SubtitleExtractChunkTest(SubtitleExtractBase):
    @classmethod
    def setUpClass(cls):
        _require_tools()
        cls._tmp = tempfile.mkdtemp()
        cls.long_video = Path(cls._tmp) / "long.mp4"
        _make_video(cls.long_video, seconds=3)

    def test_large_video_segments_and_dedups(self):
        (self.job_dir / "source.mp4").write_bytes(self.long_video.read_bytes())
        self._write_meta(3.0)
        self._set_keys(["key-one"])

        def fake_call(key, prompt, video_path, offset_sec):
            if offset_sec == 0.0:
                return [
                    {"text": "dup", "start_sec": 0.5, "end_sec": 1.5},
                    {"text": "uniqueA", "start_sec": 1.6, "end_sec": 1.9},
                ]
            return [
                {"text": "dup", "start_sec": 1.5, "end_sec": 2.4},
                {"text": "uniqueB", "start_sec": 2.5, "end_sec": 2.8},
            ]

        with (
            mock.patch.object(subtitle_extract, "_call_gemini", side_effect=fake_call),
            mock.patch("pipeline.config.LONG_VIDEO_CHUNK_THRESHOLD_SEC", 2.0),
            mock.patch("pipeline.config.SUBTITLE_OVERLAP_SEC", 1.0),
        ):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["chunked"])
        self.assertEqual(result["segments_count"], 2)
        self.assertEqual(result["failed_segments"], [])
        texts = [s["text"] for s in result["subtitles"]]
        self.assertEqual(texts, ["dup", "uniqueA", "uniqueB"])
        dup = [s for s in result["subtitles"] if s["text"] == "dup"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["start_sec"], 0.5)

    def test_round_robin_across_segments(self):
        (self.job_dir / "source.mp4").write_bytes(self.long_video.read_bytes())
        self._write_meta(3.0)
        self._set_keys(["key-a", "key-b"])
        calls = []

        def fake_call(key, prompt, video_path, offset_sec):
            calls.append((key, offset_sec))
            return [{"text": "t", "start_sec": offset_sec + 0.2,
                     "end_sec": offset_sec + 1.0}]

        with (
            mock.patch.object(subtitle_extract, "_call_gemini", side_effect=fake_call),
            mock.patch("pipeline.config.LONG_VIDEO_CHUNK_THRESHOLD_SEC", 2.0),
            mock.patch("pipeline.config.SUBTITLE_OVERLAP_SEC", 1.0),
        ):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, [("key-a", 0.0), ("key-b", 1.0)])

    def test_default_threshold_90s_chunks_150s_video(self):
        # C2: with the production default LONG_VIDEO_CHUNK_THRESHOLD_SEC=90, a
        # ~2.5-minute (150s) video — previously under the old 600s default and
        # sent whole — now extracts as chunked=True. Duration comes straight
        # from job_meta.json (no real long ffmpeg fixture needed); segments
        # and Gemini are mocked.
        self._write_meta(150.0)
        self._set_keys(["key-one"])

        def fake_call(key, prompt, video_path, offset_sec):
            return [{"text": "t", "start_sec": offset_sec + 0.2,
                     "end_sec": offset_sec + 1.0}]

        with (
            mock.patch.object(subtitle_extract, "_call_gemini", side_effect=fake_call),
            mock.patch(
                "pipeline.subtitle_extract._segment_video",
                return_value=[
                    {"index": 0, "start": 0.0, "path": self.job_dir / "s0.mp4"},
                    {"index": 1, "start": 90.0, "path": self.job_dir / "s1.mp4"},
                ],
            ),
        ):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["chunked"])
        self.assertEqual(result["segments_count"], 2)
        self.assertEqual(result["failed_segments"], [])


class SubtitleExtractFailureTest(SubtitleExtractBase):
    def test_key_rotation_falls_back(self):
        self._write_meta(30.0)
        self._set_keys(["key-bad", "key-good"])
        calls = []

        def fake_call(key, prompt, video_path, offset_sec):
            calls.append(key)
            if key == "key-bad":
                raise RuntimeError("boom")
            return [{"text": "X", "start_sec": offset_sec + 0.1,
                     "end_sec": offset_sec + 1.0}]

        with mock.patch.object(subtitle_extract, "_call_gemini", side_effect=fake_call):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(calls, ["key-bad", "key-good"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["subtitles"]), 1)

    def test_all_keys_fail_signal_no_exception(self):
        self._write_meta(30.0)
        self._set_keys(["key-bad", "key-worse"])

        def fake_call(*args, **kwargs):
            raise RuntimeError("total failure")

        with mock.patch.object(subtitle_extract, "_call_gemini", side_effect=fake_call):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["status"], "extraction_failed")
        self.assertEqual(result["failed_segments"], [0])
        self.assertEqual(result["subtitles"], [])
        self.assertEqual(self._read_result()["status"], "extraction_failed")

    def test_no_active_keys_signal(self):
        self._write_meta(30.0)
        with mock.patch.object(subtitle_extract, "_call_gemini") as fake:
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )
        fake.assert_not_called()
        self.assertEqual(result["status"], "extraction_failed")
        self.assertEqual(result["subtitles"], [])

    def test_call_budget_cap_fails_gracefully(self):
        # U2b: when the shared per-job CallBudget is exhausted the extraction
        # must degrade to extraction_failed (error recorded), never raise.
        self._write_meta(30.0)
        self._set_keys(["key-a", "key-b"])
        with mock.patch.object(
            subtitle_extract, "_call_gemini",
            side_effect=RuntimeError("boom"),
        ):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root,
                call_budget=CallBudget(max_calls=0),
            )

        self.assertEqual(result["status"], "extraction_failed")
        self.assertEqual(result["failed_segments"], [0])
        self.assertEqual(result["errors"]["0"]["type"], "call_budget_exceeded")
        self.assertEqual(result["errors"]["0"]["max_calls"], 0)


class SubtitleExtractRetryTest(SubtitleExtractBase):
    def test_rate_limit_single_key_fails_without_backoff(self):
        # U2b: rotation is now classification-based (call_with_rotation_v2) —
        # a 429 is "rotatable" and rotates immediately, there is no same-key
        # backoff any more, so with a single key the segment fails cleanly.
        self._write_meta(30.0)
        self._set_keys(["key-one"])
        calls = []

        def fake_call(key, prompt, video_path, offset_sec):
            calls.append(key)
            raise genai_errors.ClientError(code=429, response_json={})

        with mock.patch.object(
            subtitle_extract, "_call_gemini", side_effect=fake_call
        ):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["status"], "extraction_failed")
        self.assertEqual(calls, ["key-one"])
        self.assertEqual(result["failed_segments"], [0])
        self.assertEqual(result["errors"]["0"]["type"], "rate_limit")

    def test_rate_limit_rotates_immediately(self):
        self._write_meta(30.0)
        self._set_keys(["key-a", "key-b"])
        calls = []

        def fake_call(key, prompt, video_path, offset_sec):
            calls.append(key)
            raise genai_errors.ClientError(code=429, response_json={})

        with mock.patch.object(
            subtitle_extract, "_call_gemini", side_effect=fake_call
        ):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["status"], "extraction_failed")
        self.assertEqual(calls, ["key-a", "key-b"])
        self.assertEqual(result["failed_segments"], [0])
        self.assertEqual(result["errors"]["0"]["type"], "rate_limit")

    def test_non_rotatable_error_stops_without_rotation(self):
        # U2b: a 400 is classified "non_rotatable" — the very first key's
        # failure stops the rotation, the second key is never touched.
        self._write_meta(30.0)
        self._set_keys(["key-bad", "key-good"])
        calls = []

        def fake_call(key, prompt, video_path, offset_sec):
            calls.append(key)
            raise genai_errors.ClientError(code=400, response_json={})

        with mock.patch.object(
            subtitle_extract, "_call_gemini", side_effect=fake_call
        ):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["status"], "extraction_failed")
        self.assertEqual(calls, ["key-bad"])
        self.assertEqual(result["errors"]["0"]["type"], "non_rotatable")

    def test_content_blocked_no_retry_no_rotation(self):
        self._write_meta(30.0)
        self._set_keys(["key-a", "key-b"])
        calls = []

        def fake_call(key, prompt, video_path, offset_sec):
            calls.append(key)
            raise subtitle_extract.ContentBlockedError("SAFETY", "blocked by policy")

        with mock.patch.object(
            subtitle_extract, "_call_gemini", side_effect=fake_call
        ):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["status"], "extraction_failed")
        self.assertEqual(calls, ["key-a"])
        self.assertEqual(result["errors"]["0"]["type"], "content_blocked")
        self.assertIn("SAFETY", result["errors"]["0"]["message"])

    def test_errors_persisted_in_saved_json(self):
        self._write_meta(30.0)
        self._set_keys(["key-bad"])
        with mock.patch.object(
            subtitle_extract, "_call_gemini",
            side_effect=RuntimeError("real cause"),
        ):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=self.upload_root
            )
        saved = self._read_result()
        self.assertEqual(saved["errors"]["0"]["message"], "real cause")
        self.assertEqual(saved["errors"]["0"]["type"], "permanent")
        self.assertEqual(result["errors"]["0"]["message"], "real cause")


class ExtractWindowTest(SubtitleExtractBase):
    def test_success_applies_absolute_offset(self):
        self._write_meta(120.0)
        self._set_keys(["key-one"])

        def fake_call(key, prompt, video_path, offset_sec):
            self.assertEqual(offset_sec, 45.0)
            return [
                {"text": "a", "start_sec": offset_sec + 1.0,
                 "end_sec": offset_sec + 2.5},
                {"text": "b", "start_sec": offset_sec + 3.0,
                 "end_sec": offset_sec + 4.0},
            ]

        with (
            mock.patch.object(subtitle_extract, "_run_ffmpeg"),
            mock.patch.object(subtitle_extract, "_call_gemini", side_effect=fake_call),
        ):
            subs = subtitle_extract.extract_window(
                self.job_id, 45.0, 75.0, upload_root=self.upload_root
            )

        self.assertEqual(len(subs), 2)
        self.assertEqual(subs[0]["text"], "a")
        self.assertEqual(subs[0]["start_sec"], 46.0)
        self.assertEqual(subs[0]["end_sec"], 47.5)
        self.assertEqual(subs[1]["start_sec"], 48.0)
        self.assertEqual(subs[1]["end_sec"], 49.0)

    def test_all_keys_fail_returns_none(self):
        self._write_meta(120.0)
        self._set_keys(["key-bad", "key-worse"])

        def fake_call(*args, **kwargs):
            raise RuntimeError("total failure")

        with (
            mock.patch.object(subtitle_extract, "_run_ffmpeg"),
            mock.patch.object(subtitle_extract, "_call_gemini", side_effect=fake_call),
        ):
            result = subtitle_extract.extract_window(
                self.job_id, 10.0, 20.0, upload_root=self.upload_root
            )

        self.assertIsNone(result)

    def test_malformed_json_returns_none(self):
        self._write_meta(120.0)
        self._set_keys(["key-one"])

        with (
            mock.patch.object(subtitle_extract, "_run_ffmpeg"),
            mock.patch.object(
                subtitle_extract, "_call_gemini",
                side_effect=ValueError("malformed JSON from Gemini"),
            ),
        ):
            result = subtitle_extract.extract_window(
                self.job_id, 10.0, 20.0, upload_root=self.upload_root
            )

        self.assertIsNone(result)

    def test_ffmpeg_args_use_ss_to_copy(self):
        self._write_meta(120.0)
        self._set_keys(["key-one"])
        fake_ffmpeg_result = type(
            "R", (), {"returncode": 0, "stderr": "", "stdout": ""}
        )()
        clip_path = None

        def fake_call(key, prompt, video_path, offset_sec):
            nonlocal clip_path
            clip_path = str(video_path)
            return [{"text": "x", "start_sec": offset_sec + 0.1,
                     "end_sec": offset_sec + 1.0}]

        with (
            mock.patch.object(subtitle_extract.subprocess, "run",
                              return_value=fake_ffmpeg_result) as fake_run,
            mock.patch.object(subtitle_extract, "_call_gemini", side_effect=fake_call),
        ):
            subs = subtitle_extract.extract_window(
                self.job_id, 30.0, 60.0, upload_root=self.upload_root
            )

        self.assertIsNotNone(subs)
        run_calls = fake_run.call_args_list
        ffmpeg_args = run_calls[0].args[0]
        self.assertEqual(ffmpeg_args[0], "ffmpeg")
        self.assertIn("-ss", ffmpeg_args)
        self.assertIn("-to", ffmpeg_args)
        self.assertIn("-c", ffmpeg_args)
        self.assertEqual(ffmpeg_args[ffmpeg_args.index("-c") + 1], "copy")
        self.assertIn("30.000", ffmpeg_args)
        self.assertIn("60.000", ffmpeg_args)
        self.assertTrue(clip_path.startswith(str(self.job_dir / "repair_segments")))
        self.assertTrue(clip_path.endswith(".mp4"))

    def test_ffmpeg_failure_returns_none(self):
        self._write_meta(120.0)
        self._set_keys(["key-one"])
        with (
            mock.patch.object(
                subtitle_extract, "_run_ffmpeg",
                side_effect=RuntimeError("ffmpeg failed"),
            ),
            mock.patch.object(subtitle_extract, "_call_gemini") as fake,
        ):
            result = subtitle_extract.extract_window(
                self.job_id, 10.0, 20.0, upload_root=self.upload_root
            )
        self.assertIsNone(result)
        fake.assert_not_called()

    def test_missing_source_returns_none(self):
        (self.job_dir / "source.mp4").unlink()
        self._write_meta(120.0)
        result = subtitle_extract.extract_window(
            self.job_id, 0.0, 10.0, upload_root=self.upload_root
        )
        self.assertIsNone(result)


class UploadReuseTest(unittest.TestCase):
    def test_upload_cached_per_key_and_path(self):
        # U2b: same-key same-path uploads are cached (no re-upload); a second
        # key or a changed file gets its own upload. This is the remaining
        # purpose of _UPLOAD_CACHE now that same-key rate-limit retries are
        # gone (rotation just moves to the next key).
        class FakeUploaded:
            uri = "files/fake-upload"
            mime_type = "video/mp4"

        class FakeFiles:
            def __init__(self):
                self.uploads = 0

            def upload(self, file=None):
                self.uploads += 1
                return FakeUploaded()

        class FakeClient:
            def __init__(self, api_key):
                self.api_key = api_key
                self.files = FakeFiles()

        tmp = tempfile.mkdtemp()
        video = Path(tmp) / "seg.mp4"
        video.write_bytes(b"dummy")

        subtitle_extract._UPLOAD_CACHE.clear()
        self.addCleanup(subtitle_extract._UPLOAD_CACHE.clear)

        client_k1 = FakeClient("k1")
        with mock.patch.object(subtitle_extract.genai, "Client", return_value=client_k1):
            first = subtitle_extract._get_or_upload(client_k1, "k1", video)
            second = subtitle_extract._get_or_upload(client_k1, "k1", video)
        self.assertIs(first, second)
        self.assertEqual(client_k1.files.uploads, 1)

        client_k2 = FakeClient("k2")
        with mock.patch.object(subtitle_extract.genai, "Client", return_value=client_k2):
            third = subtitle_extract._get_or_upload(client_k2, "k2", video)
        self.assertIsNot(first, third)
        self.assertEqual(client_k2.files.uploads, 1)


class ContentBlockDetectionTest(unittest.TestCase):
    def test_prompt_feedback_block_reason(self):
        class Feedback:
            block_reason = genai_types.BlockedReason.SAFETY
            block_reason_message = "hit safety"

        class Resp:
            prompt_feedback = Feedback()
            candidates = []

        reason = subtitle_extract._is_content_blocked(None, Resp())
        self.assertEqual(reason["reason"], "SAFETY")
        self.assertEqual(reason["message"], "hit safety")

    def test_candidate_finish_reason_safety(self):
        class Resp:
            prompt_feedback = None
            candidates = [
                type("C", (), {"finish_reason": genai_types.FinishReason.SAFETY})()
            ]

        reason = subtitle_extract._is_content_blocked(None, Resp())
        self.assertEqual(reason["reason"], "SAFETY")

    def test_unblocked_response_is_none(self):
        class Resp:
            prompt_feedback = None
            candidates = []

        self.assertIsNone(subtitle_extract._is_content_blocked(None, Resp()))

    def test_rate_limit_error_is_not_content_blocked(self):
        exc = genai_errors.ClientError(code=429, response_json={})
        self.assertIsNone(subtitle_extract._is_content_blocked(exc))


class ParseTest(unittest.TestCase):
    def test_parse_subtitles_applies_offset(self):
        text = json.dumps({
            "subtitles": [
                {"text": "a", "start_sec": 1.0, "end_sec": 2.5},
            ]
        })
        subs = subtitle_extract._parse_subtitles(text, offset_sec=60.0)
        self.assertEqual(subs[0]["start_sec"], 61.0)
        self.assertEqual(subs[0]["end_sec"], 62.5)

    def test_extract_json_tolerates_fences(self):
        text = '```json\n{"subtitles": []}\n```'
        data = subtitle_extract._extract_json(text)
        self.assertEqual(data, {"subtitles": []})

    def test_malformed_json_raises(self):
        with self.assertRaises(ValueError):
            subtitle_extract._parse_subtitles("not json at all", 0.0)

    def test_partial_failure_signal(self):
        tmp = tempfile.mkdtemp()
        upload_root = Path(tmp) / "uploads"
        job_dir = upload_root / "job-p"
        job_dir.mkdir(parents=True)
        (job_dir / "source.mp4").write_bytes(b"x")
        (job_dir / "job_meta.json").write_text(
            json.dumps({"duration_sec": 3.0}), encoding="utf-8"
        )
        store = Path(tmp) / "keys.json"
        store.write_text(json.dumps({"keys": [{"id": "k1", "key": "k"}]}))
        with (
            mock.patch.object(key_store, "KEY_STORE_PATH", store),
            mock.patch("pipeline.config.LONG_VIDEO_CHUNK_THRESHOLD_SEC", 2.0),
            mock.patch("pipeline.config.SUBTITLE_OVERLAP_SEC", 1.0),
            mock.patch.object(subtitle_extract, "_call_gemini", side_effect=RuntimeError("x")),
            mock.patch("pipeline.subtitle_extract._segment_video"),
        ):
            subtitle_extract._segment_video.return_value = [
                {"index": 0, "start": 0.0, "path": job_dir / "s0.mp4"},
                {"index": 1, "start": 1.0, "path": job_dir / "s1.mp4"},
                {"index": 2, "start": 2.0, "path": job_dir / "s2.mp4"},
            ]
            result = subtitle_extract.extract_subtitles("job-p", upload_root=upload_root)

        self.assertEqual(result["status"], "extraction_failed")
        self.assertEqual(result["failed_segments"], [0, 1, 2])
        self.assertEqual(result["subtitles"], [])


if __name__ == "__main__":
    unittest.main()
