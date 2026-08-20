"""Tests for pipeline.voiceover_auto (Gemini TTS auto voiceover)."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import key_store, video_ingest, voiceover_auto
from pipeline import config
from pipeline.gemini_rotation import CallBudget


def _wav_bytes(duration_sec):
    """Build a real mono wav of the given duration and return its bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clip.wav"
        voiceover_auto._make_silence(duration_sec, path)
        return path.read_bytes()


def _make_subtitles(job_dir, entries):
    (job_dir / "subtitles_hi.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )


def _map_tts(mapping):
    """Return a callable TTS mock that looks up ``text`` in ``mapping``.

    Values may be bytes (returned as audio) or callables (invoked; the result
    is returned, so a raising callable simulates a TTS failure).
    """
    def fake_tts(key, text, voice_name):
        value = mapping[text]
        if callable(value):
            return value()
        return value
    return fake_tts


class VoiceoverAutoBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-d2"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

        self.clip_short = _wav_bytes(1.0)
        self.clip_long = _wav_bytes(2.5)

        self._keys_patch = mock.patch.object(
            key_store, "get_active_keys", return_value=["k1", "k2"]
        )
        self._keys_patch.start()
        self.addCleanup(self._keys_patch.stop)

    def _run(self, tts_mock):
        with mock.patch.object(voiceover_auto, "_call_tts", side_effect=tts_mock):
            return voiceover_auto.generate_auto_voiceover(
                self.job_id, upload_root=self.upload_root
            )

    def _timestamps(self):
        path = self.job_dir / "timestamps_hi_auto.json"
        return json.loads(path.read_text(encoding="utf-8"))


class VoiceoverAutoModuleTest(VoiceoverAutoBase):
    def test_happy_path_deterministic_timing(self):
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "पहला"},
                {"serial": 2, "text_zh": "B", "text_hi": "दूसरा"},
            ],
        )
        result = self._run(_map_tts({"पहला": self.clip_short, "दूसरा": self.clip_long}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["failed_serials"], [])
        self.assertEqual(result["entries_count"], 2)

        timestamps = self._timestamps()
        self.assertEqual(timestamps[0]["serial"], 1)
        self.assertEqual(timestamps[0]["start_sec"], 0.0)
        self.assertAlmostEqual(timestamps[0]["end_sec"], 1.0, places=2)
        self.assertFalse(timestamps[0]["tts_failed"])
        self.assertEqual(timestamps[1]["serial"], 2)
        self.assertAlmostEqual(timestamps[1]["start_sec"], 1.0, places=2)
        self.assertAlmostEqual(timestamps[1]["end_sec"], 3.5, places=2)
        self.assertFalse(timestamps[1]["tts_failed"])

        self.assertTrue((self.job_dir / "voiceover_hi.wav").exists())
        self.assertAlmostEqual(result["total_sec"], 3.5, places=1)

    def test_tts_failure_uses_silence_and_flags(self):
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "एक"},
                {"serial": 2, "text_zh": "B", "text_hi": "दो"},
            ],
        )

        def boom():
            raise RuntimeError("simulated TTS failure")

        result = self._run(_map_tts({"एक": boom, "दो": boom}))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_serials"], [1, 2])

        timestamps = self._timestamps()
        for entry in timestamps:
            self.assertTrue(entry["tts_failed"])
            self.assertAlmostEqual(
                entry["end_sec"] - entry["start_sec"],
                config.TTS_FAIL_SILENCE_SEC,
                places=2,
            )
        self.assertAlmostEqual(timestamps[1]["start_sec"], 2.0, places=2)
        self.assertTrue((self.job_dir / "voiceover_hi.wav").exists())

    def test_partial_failure_continues(self):
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "अ"},
                {"serial": 2, "text_zh": "B", "text_hi": "ब"},
                {"serial": 3, "text_zh": "C", "text_hi": "स"},
            ],
        )

        def fail_second():
            raise RuntimeError("boom")

        result = self._run(
            _map_tts(
                {"अ": self.clip_short, "ब": fail_second, "स": self.clip_long}
            )
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_serials"], [2])

        timestamps = self._timestamps()
        self.assertFalse(timestamps[0]["tts_failed"])
        self.assertTrue(timestamps[1]["tts_failed"])
        self.assertFalse(timestamps[2]["tts_failed"])
        self.assertAlmostEqual(timestamps[1]["start_sec"], 1.0, places=2)
        self.assertAlmostEqual(timestamps[2]["start_sec"], 3.0, places=2)

    def test_no_active_keys_returns_unavailable(self):
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "पहला"}],
        )
        with mock.patch.object(key_store, "get_active_keys", return_value=[]):
            result = voiceover_auto.generate_auto_voiceover(
                self.job_id, upload_root=self.upload_root
            )
        self.assertEqual(result["status"], "tts_unavailable")
        self.assertIsNone(result["voiceover_path"])
        self.assertFalse((self.job_dir / "voiceover_hi.wav").exists())
        self.assertFalse((self.job_dir / "timestamps_hi_auto.json").exists())

    def test_call_budget_cap_uses_silence_without_raising(self):
        # U2b: an exhausted per-job CallBudget must degrade to silence
        # placeholders + tts_failed flags (partial), never raise.
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "एक"},
                {"serial": 2, "text_zh": "B", "text_hi": "दो"},
            ],
        )
        with mock.patch.object(voiceover_auto, "_call_tts") as fake:
            result = voiceover_auto.generate_auto_voiceover(
                self.job_id, upload_root=self.upload_root,
                call_budget=CallBudget(max_calls=0),
            )
        fake.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_serials"], [1, 2])
        for entry in self._timestamps():
            self.assertTrue(entry["tts_failed"])

    def test_missing_input_raises(self):
        with self.assertRaises(FileNotFoundError):
            voiceover_auto.generate_auto_voiceover(
                self.job_id, upload_root=self.upload_root
            )

    def test_empty_entries_writes_empty_timestamps(self):
        _make_subtitles(self.job_dir, [])
        result = self._run(_map_tts({}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self._timestamps(), [])

    def test_empty_text_hi_uses_silence(self):
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "  "}],
        )
        result = self._run(_map_tts({}))
        self.assertEqual(result["status"], "partial")
        entry = self._timestamps()[0]
        self.assertTrue(entry["tts_failed"])
        self.assertAlmostEqual(entry["end_sec"], 2.0, places=2)

    def test_second_pass_repairs_first_pass_failure(self):
        # U3b: serial 2 fails on every key in the first pass, but succeeds on
        # the bounded second pass -> its silence placeholder is replaced with
        # the real audio and tts_failed is cleared. Two keys are configured, so
        # "first pass" = the first two TTS attempts for that serial, "second
        # pass" = the third.
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "पहला"},
                {"serial": 2, "text_zh": "B", "text_hi": "दूसरा"},
            ],
        )
        attempts = {"दूसरा": 0}

        def flaky():
            attempts["दूसरा"] += 1
            if attempts["दूसरा"] <= 2:
                raise RuntimeError("transient TTS failure")
            return self.clip_long

        result = self._run(
            _map_tts({"पहला": self.clip_short, "दूसरा": flaky})
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["failed_serials"], [])

        timestamps = self._timestamps()
        self.assertFalse(timestamps[0]["tts_failed"])
        self.assertFalse(timestamps[1]["tts_failed"])
        # The repaired clip is the real 2.5s audio, not the 2.0s silence, so
        # the cumulative end_sec reflects the real duration.
        self.assertAlmostEqual(timestamps[1]["start_sec"], 1.0, places=2)
        self.assertAlmostEqual(timestamps[1]["end_sec"], 3.5, places=2)
        self.assertAlmostEqual(result["total_sec"], 3.5, places=1)

    def test_second_pass_persistent_failure_keeps_silence(self):
        # U3b: a serial that fails on every key in BOTH passes stays failed for
        # good — silence placeholder remains, tts_failed stays true.
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "पहला"},
                {"serial": 2, "text_zh": "B", "text_hi": "दूसरा"},
            ],
        )

        def always_fail():
            raise RuntimeError("persistent TTS failure")

        result = self._run(
            _map_tts({"पहला": self.clip_short, "दूसरा": always_fail})
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_serials"], [2])

        timestamps = self._timestamps()
        self.assertFalse(timestamps[0]["tts_failed"])
        self.assertTrue(timestamps[1]["tts_failed"])
        self.assertAlmostEqual(
            timestamps[1]["end_sec"] - timestamps[1]["start_sec"],
            config.TTS_FAIL_SILENCE_SEC,
            places=2,
        )

    def test_resume_reuses_existing_clips(self):
        # U1c resumability: a second run must reuse clips that already have a
        # real TTS duration (>= TTS_FAIL_SILENCE_SEC + tolerance) and only
        # regenerate the ones that are missing or too short (silence-sized).
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "पहला"},
                {"serial": 2, "text_zh": "B", "text_hi": "दूसरा"},
            ],
        )
        first = self._run(
            _map_tts({"पहला": self.clip_short, "दूसरा": self.clip_long})
        )
        self.assertEqual(first["status"], "ok")
        self.assertTrue((self.job_dir / "auto_tts_clips" / "serial_1.wav").exists())
        self.assertTrue((self.job_dir / "auto_tts_clips" / "serial_2.wav").exists())

        calls = []

        def recording_tts(text):
            def _record():
                calls.append(text)
                return self.clip_long
            return _record

        second = self._run(
            _map_tts(
                {
                    "पहला": recording_tts("पहला"),
                    "दूसरा": recording_tts("दूसरा"),
                }
            )
        )
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["failed_serials"], [])
        # serial_1 clip is only 1.0s (silence-sized) -> regenerated; serial_2
        # clip is 2.5s (real TTS) -> reused, so its text is never re-TTS'd.
        self.assertEqual(calls, ["पहला"])
        timestamps = self._timestamps()
        self.assertFalse(timestamps[1]["tts_failed"])


class VoiceSelectionTest(VoiceoverAutoBase):
    """F12f: the TTS voice name follows the job's target_lang."""

    def _run_recording(self, voices):
        with mock.patch.object(
            voiceover_auto,
            "_call_tts",
            side_effect=lambda key, text, voice_name: (
                voices.append(voice_name), self.clip_short
            )[1],
        ):
            return voiceover_auto.generate_auto_voiceover(
                self.job_id, upload_root=self.upload_root
            )

    def test_hi_default_job_uses_hindi_voice(self):
        # No job_config.json -> default target_lang hi -> pre-F12f voice.
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_translated": "पहला"}],
        )
        voices = []
        self._run_recording(voices)
        self.assertEqual(voices, [config.TTS_VOICE_HINDI])

    def test_bn_job_uses_bn_voice(self):
        (self.job_dir / "job_config.json").write_text(
            json.dumps({"job_id": self.job_id, "target_lang": "bn"}),
            encoding="utf-8",
        )
        (self.job_dir / "subtitles_bn.json").write_text(
            json.dumps(
                [{"serial": 1, "text_zh": "A", "text_translated": "নমস্কার"}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        voices = []
        result = self._run_recording(voices)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(voices, [config.TTS_VOICE_BN])
        self.assertTrue((self.job_dir / "voiceover_bn.wav").exists())

    def test_en_job_uses_en_voice(self):
        (self.job_dir / "job_config.json").write_text(
            json.dumps({"job_id": self.job_id, "target_lang": "en"}),
            encoding="utf-8",
        )
        (self.job_dir / "subtitles_en.json").write_text(
            json.dumps(
                [{"serial": 1, "text_zh": "A", "text_translated": "Hello"}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        voices = []
        self._run_recording(voices)
        self.assertEqual(voices, [config.TTS_VOICE_EN])


class VoiceoverAutoEndpointTest(VoiceoverAutoBase):
    def setUp(self):
        super().setUp()
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore)
        self.client = TestClient(app)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def _wait_for_stage(self, stage, timeout=10.0, interval=0.1):
        """Poll /api/jobs/{job_id}/status until the given stage is done.

        U1c runs D2 -> E2 on a background thread, so the auto_tts page shows an
        intermediate processing page first. The TTS mock must stay active while
        the thread runs, which is why callers keep the mock.patch block open
        around this wait.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.client.get(f"/api/jobs/{self.job_id}/status")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            stage_info = (body.get("stages") or {}).get(stage)
            if stage_info and stage_info.get("state") == "done":
                return body
            if stage_info and stage_info.get("state") == "error":
                self.fail(f"stage {stage} errored: {stage_info}")
            time.sleep(interval)
        self.fail(f"stage {stage} for {self.job_id} did not finish in {timeout}s")

    def test_auto_page_generates_and_links(self):
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "पहला"},
                {"serial": 2, "text_zh": "B", "text_hi": "दूसरा"},
            ],
        )
        # U1c: the first GET returns the intermediate processing page and starts
        # a background thread; the done page (identical markup to pre-U1c) is
        # served on a later GET once the thread finishes.
        with mock.patch.object(
            voiceover_auto,
            "_call_tts",
            side_effect=_map_tts({"पहला": self.clip_short, "दूसरा": self.clip_long}),
        ):
            first = self.client.get(f"/voiceover/{self.job_id}/auto_tts")
            self.assertEqual(first.status_code, 200)
            self.assertIn("Processing", first.text)
            self._wait_for_stage("voiceover_auto")
            res = self.client.get(f"/voiceover/{self.job_id}/auto_tts")
        self.assertEqual(res.status_code, 200)
        self.assertIn("voiceover_hi.wav", res.text)
        self.assertIn("timestamps_hi_auto.json", res.text)
        self.assertIn("<strong>ok</strong>", res.text)

    def test_auto_page_unknown_job_404(self):
        res = self.client.get("/voiceover/missing-job/auto_tts")
        self.assertEqual(res.status_code, 404)

    def test_auto_page_done_short_circuits_no_rerun(self):
        # U1c resume at the endpoint level: once the voiceover_auto stage is
        # done, a later GET renders the stored result synchronously without
        # re-invoking TTS (no re-TTS, no re-render work).
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "पहला"},
                {"serial": 2, "text_zh": "B", "text_hi": "दूसरा"},
            ],
        )
        with mock.patch.object(
            voiceover_auto,
            "_call_tts",
            side_effect=_map_tts({"पहला": self.clip_short, "दूसरा": self.clip_long}),
        ):
            self.client.get(f"/voiceover/{self.job_id}/auto_tts")
            self._wait_for_stage("voiceover_auto")

        calls = []

        def recording_tts(key, text, voice_name):
            calls.append(text)
            return self.clip_long

        with mock.patch.object(
            voiceover_auto,
            "_call_tts",
            side_effect=_map_tts({"पहला": recording_tts, "दूसरा": recording_tts}),
        ):
            res = self.client.get(f"/voiceover/{self.job_id}/auto_tts")
        self.assertEqual(res.status_code, 200)
        self.assertIn("<strong>ok</strong>", res.text)
        self.assertEqual(calls, [])

    def test_download_voiceover_wav(self):
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "पहला"}],
        )
        with mock.patch.object(
            voiceover_auto,
            "_call_tts",
            side_effect=_map_tts({"पहला": self.clip_short}),
        ):
            self.client.get(f"/voiceover/{self.job_id}/auto_tts")
            self._wait_for_stage("voiceover_auto")
        res = self.client.get(f"/download/{self.job_id}/voiceover?format=wav")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["content-type"], "audio/wav")

    def test_download_voiceover_timestamps(self):
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "पहला"}],
        )
        with mock.patch.object(
            voiceover_auto,
            "_call_tts",
            side_effect=_map_tts({"पहला": self.clip_short}),
        ):
            self.client.get(f"/voiceover/{self.job_id}/auto_tts")
            self._wait_for_stage("voiceover_auto")
        res = self.client.get(
            f"/download/{self.job_id}/voiceover?format=timestamps"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data[0]["serial"], 1)
        self.assertFalse(data[0]["tts_failed"])

    def test_download_voiceover_bad_format_400(self):
        res = self.client.get(
            f"/download/{self.job_id}/voiceover?format=bogus"
        )
        self.assertEqual(res.status_code, 400)


class WrapPcmWavTest(unittest.TestCase):
    """F23: the TTS API returns raw L16 PCM — _wrap_pcm_wav adds the header."""

    def test_wraps_raw_pcm_in_valid_wav(self):
        import io
        import wave

        pcm = bytes(range(0, 256)) * 10  # 2560 bytes of 16-bit samples
        out = voiceover_auto._wrap_pcm_wav(pcm, 24000)
        self.assertTrue(out.startswith(b"RIFF"), "must be a WAV container")
        with wave.open(io.BytesIO(out)) as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 24000)
            self.assertEqual(wav.readframes(1280), pcm)

    def test_wrap_is_lossless(self):
        pcm = b"\x01\x00\x02\x00\x03\x00"
        out = voiceover_auto._wrap_pcm_wav(pcm, 16000)
        import io
        import wave

        with wave.open(io.BytesIO(out)) as wav:
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.readframes(3), pcm)


if __name__ == "__main__":
    unittest.main()
