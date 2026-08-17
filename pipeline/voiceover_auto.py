"""Automatic voiceover generation via Gemini TTS (target-language text).

For every serial in ``subtitles_<target_lang>.json`` the translated text is
sent to Gemini TTS in its own request (key rotation reused from
``subtitle_extract.call_with_rotation``). Each resulting clip's real duration
is measured with ffprobe, clips are concatenated in serial order, and
per-line cumulative timestamps are written to
``timestamps_<target_lang>_auto.json``.

Resilience rules:

- A TTS failure (after every active key was tried) does not stop the job:
  a short silence placeholder is used for that serial and the entry is
  flagged ``tts_failed: true`` in the timestamps file.
- The failed serials get one bounded second pass (U3b): each one is tried
  once more with the TTS, carrying on the same key rotation and the same
  shared ``CallBudget`` from where the main loop left off. A serial that
  succeeds on the second pass gets its silence placeholder replaced with the
  real audio and ``tts_failed`` cleared; only serials that fail both passes
  stay flagged. The cumulative timestamps are recomputed from the final clip
  durations afterwards, because a repaired clip's real duration usually
  differs from the silence placeholder.
- If no active Gemini key is configured the job is reported as
  ``tts_unavailable`` and no audio is produced.
- ffmpeg/ffprobe failures on the produced clips are treated the same way
  (silence placeholder) so a single bad clip never aborts the whole job.
"""

import json
import logging
import subprocess
import tempfile
from pathlib import Path

from google import genai
from google.genai import types as genai_types

from pipeline import config, job_logging, key_store, lang_files, subtitle_extract, video_ingest

logger = logging.getLogger(__name__)

CLIP_DIR_NAME = "auto_tts_clips"


def _call_tts(key, text, voice_name):
    """Send one line to Gemini TTS and return the raw audio bytes.

    ``voice_name`` is the ``config.TTS_VOICES[target_lang]`` style voice
    resolved by the caller; the spoken language itself comes from ``text``.
    """
    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=config.TTS_MODEL,
        contents=text,
        config=genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            ),
        ),
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data
    raise RuntimeError("TTS response contained no audio")


def _run(cmd, timeout):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"ffmpeg/ffprobe failed: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg/ffprobe error: {result.stderr.strip()}")
    return result


def _probe_audio_duration(path):
    """Return the real duration (seconds) of an audio file via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    result = _run(cmd, 60)
    return float(result.stdout.strip())


def _make_silence(duration_sec, out_path):
    """Write a mono silence placeholder clip."""
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"anullsrc=r={config.TTS_SAMPLE_RATE}:cl=mono",
        "-t", str(duration_sec), "-c:a", "pcm_s16le", str(out_path),
    ]
    _run(cmd, 60)


def _normalize_and_concat(clip_paths, out_path):
    """Decode every clip to a common PCM format and concatenate in order."""
    if not clip_paths:
        raise ValueError("no clips to concatenate")
    with tempfile.TemporaryDirectory(dir=str(out_path.parent)) as tmpdir:
        wavs = []
        for index, clip in enumerate(clip_paths):
            wav = Path(tmpdir) / f"{index:05d}.wav"
            _run(
                [
                    "ffmpeg", "-y", "-i", str(clip),
                    "-ar", str(config.TTS_SAMPLE_RATE), "-ac", "1",
                    "-c:a", "pcm_s16le", str(wav),
                ],
                120,
            )
            wavs.append(wav)
        inputs = []
        labels = []
        for index, wav in enumerate(wavs):
            inputs += ["-i", str(wav)]
            labels.append(f"[{index}:a]")
        concat = f"{''.join(labels)}concat=n={len(wavs)}:v=0:a=1[aout]"
        _run(
            [
                "ffmpeg", "-y", *inputs, "-filter_complex", concat,
                "-map", "[aout]", "-c:a", "pcm_s16le", str(out_path),
            ],
            300,
        )


def generate_auto_voiceover(job_id, upload_root=None, call_budget=None):
    """Generate the full voiceover for a job. Returns a result dict.

    The voice is picked per ``target_lang`` from ``config.TTS_VOICES`` (hi
    keeps the pre-F12f placeholder voice). Raises FileNotFoundError when the
    job has no ``subtitles_<target_lang>.json``. Never raises on per-line TTS
    failures (silence placeholders are used).
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    lang = lang_files.target_lang(job_id, upload_root)
    voice = config.TTS_VOICES.get(lang, config.TTS_VOICE_HINDI)
    sub_name = lang_files.subtitles_json(lang)
    in_path = job_dir / sub_name
    if not in_path.exists():
        raise FileNotFoundError(f"no {sub_name} for job {job_id}")

    job_logger = job_logging.get_job_logger(job_id, upload_root)
    entries = json.loads(in_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"malformed {sub_name} for job {job_id}")

    if not entries:
        timestamps_path = job_dir / lang_files.timestamps_auto(lang)
        timestamps_path.write_text("[]", encoding="utf-8")
        return {
            "job_id": job_id,
            "status": "ok",
            "failed_serials": [],
            "entries_count": 0,
            "total_sec": 0.0,
            "voiceover_path": None,
            "timestamps_path": str(timestamps_path),
        }

    keys = key_store.get_active_keys()
    if not keys:
        job_logger.error("auto voiceover cannot start for job %s: no active Gemini keys", job_id)
        return {
            "job_id": job_id,
            "status": "tts_unavailable",
            "failed_serials": [
                e.get("serial") for e in entries if lang_files.entry_text(e)
            ],
            "entries_count": len(entries),
            "total_sec": None,
            "voiceover_path": None,
            "timestamps_path": None,
        }

    clips_dir = job_dir / CLIP_DIR_NAME
    clips_dir.mkdir(parents=True, exist_ok=True)

    rotation = 0
    clip_paths = []
    durations = []
    failed_serials = []

    for entry in entries:
        serial = entry.get("serial")
        text = (lang_files.entry_text(entry) or "").strip()
        clip_path = clips_dir / f"serial_{serial}.wav"
        tts_failed = True

        # U1c resumability: reuse an existing clip if it already has positive
        # duration. Heuristic: if the clip is at least TTS_FAIL_SILENCE_SEC
        # long (plus tolerance) we treat it as a real TTS result, otherwise
        # regenerate it.
        reuse_existing = False
        if clip_path.exists():
            try:
                existing_dur = _probe_audio_duration(clip_path)
                tolerance = 0.2
                if existing_dur >= config.TTS_FAIL_SILENCE_SEC + tolerance:
                    reuse_existing = True
            except Exception:
                pass  # Bad clip -> regenerate below

        if reuse_existing:
            tts_failed = False
        elif text:
            audio, rotation, _ = subtitle_extract.call_with_rotation(
                keys, rotation, _call_tts, text, voice,
                call_budget=call_budget, logger_=job_logger,
            )
            if audio is not None:
                clip_path.write_bytes(audio)
                tts_failed = False

        if tts_failed:
            failed_serials.append(serial)
            _make_silence(config.TTS_FAIL_SILENCE_SEC, clip_path)

        try:
            duration_sec = _probe_audio_duration(clip_path)
        except Exception as exc:  # noqa: BLE001 - treat bad clip as failure
            job_logger.error("cannot probe clip for serial %s: %s", serial, exc)
            _make_silence(config.TTS_FAIL_SILENCE_SEC, clip_path)
            duration_sec = config.TTS_FAIL_SILENCE_SEC
            if serial not in failed_serials:
                failed_serials.append(serial)
            tts_failed = True

        clip_paths.append(clip_path)
        durations.append(duration_sec)

    # U3b: one bounded second pass over the serials that failed the first
    # pass. Rotation state carries on from where the main loop left it (and the
    # same shared CallBudget is reused, not a fresh one), so keys that were
    # rate-limited in pass one get a fresh chance if their quota has since
    # refreshed. Each failed serial is tried exactly once more: a success
    # replaces its silence placeholder with the real audio and drops it from
    # failed_serials, a second failure keeps it failed for good.
    if failed_serials:
        job_logger.info(
            "job %s: second bounded TTS pass over %d failed serial(s)",
            job_id, len(failed_serials),
        )
        failed_set = set(failed_serials)
        still_failed = []
        for idx, entry in enumerate(entries):
            serial = entry.get("serial")
            if serial not in failed_set:
                continue
            text = (lang_files.entry_text(entry) or "").strip()
            if not text:
                still_failed.append(serial)
                continue
            clip_path = clips_dir / f"serial_{serial}.wav"
            audio, rotation, _ = subtitle_extract.call_with_rotation(
                keys, rotation, _call_tts, text, voice,
                call_budget=call_budget, logger_=job_logger,
            )
            if audio is None:
                still_failed.append(serial)
                continue
            clip_path.write_bytes(audio)
            try:
                durations[idx] = _probe_audio_duration(clip_path)
            except Exception as exc:  # noqa: BLE001 - treat bad clip as failure
                job_logger.error(
                    "cannot probe repaired clip for serial %s: %s", serial, exc
                )
                _make_silence(config.TTS_FAIL_SILENCE_SEC, clip_path)
                durations[idx] = config.TTS_FAIL_SILENCE_SEC
                still_failed.append(serial)
        failed_serials = still_failed

    # Rebuild the cumulative timeline from the final clip durations. A
    # repaired clip's real duration can differ from the silence placeholder,
    # and E1 (edit_guideline) derives its target duration from these
    # timestamps (via timestamps_hi_final.json), so the positions must be
    # recomputed to match the final voiceover_hi.wav.
    timestamps = []
    failed_set = set(failed_serials)
    running_end = 0.0
    for idx, entry in enumerate(entries):
        serial = entry.get("serial")
        duration_sec = durations[idx]
        timestamps.append(
            {
                "serial": serial,
                "start_sec": round(running_end, 3),
                "end_sec": round(running_end + duration_sec, 3),
                "tts_failed": serial in failed_set,
            }
        )
        running_end += duration_sec

    voiceover_path = job_dir / lang_files.voiceover_audio(lang)
    _normalize_and_concat(clip_paths, voiceover_path)
    total_sec = _probe_audio_duration(voiceover_path)

    timestamps_path = job_dir / lang_files.timestamps_auto(lang)
    timestamps_path.write_text(
        json.dumps(timestamps, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "job_id": job_id,
        "status": "ok" if not failed_serials else "partial",
        "failed_serials": failed_serials,
        "entries_count": len(entries),
        "total_sec": round(total_sec, 3),
        "voiceover_path": str(voiceover_path),
        "timestamps_path": str(timestamps_path),
    }
