"""User-uploaded Hindi voiceover alignment (D3).

The user records one complete Hindi voiceover file (every ``subtitles_hi.json``
line read in serial order). The file is saved as ``uploads/<job_id>/voiceover_hi.wav``
and ``align_uploaded_voiceover`` finds, for every serial, the second-range in the
audio where that line is spoken, writing ``uploads/<job_id>/timestamps_hi_upload.json``
in the same schema D2 uses: ``[{"serial", "start_sec", "end_sec",
"alignment_fallback", "alignment_source"}]``.

Alignment strategy (F8 — Whisper is the primary timing authority):

1. Whisper transcribes the whole audio with word timestamps and the subtitle
   lines are matched onto the word stream sequentially with fuzzy matching.
   Matched serials keep Whisper's timing (``alignment_source="whisper"``).
2. Unmatched serials are offered to Gemini as a bounded secondary pass: Gemini
   sees only those lines, and a result is accepted (``alignment_source=
   "gemini_assisted"``) only when its reported end time stays within
   ``WHISPER_TAIL_TOLERANCE_SEC`` of the last Whisper-detected speech — Gemini
   can never place audio past what Whisper actually heard. Remaining unmatched
   serials get an equal-split of the gap.
3. If Whisper is unavailable / fails entirely, today's pure-Gemini flow is
   used unchanged (Gemini for every serial, Whisper fallback, then equal
   split).

Every line whose timing did not come from Whisper is flagged
``alignment_fallback: true``. ``align_uploaded_voiceover`` never raises on
Gemini/Whisper failures — the only alignment failure it treats as blocking is
an audio file with no measurable content (``total_sec <= 0``), which raises
:class:`~pipeline.voiceover_unify.VoiceoverAlignmentError`.

Duration-drift invariant (E9): whatever the alignment source, the per-serial
target timestamps are clamped to the probed audio duration (``total_sec``)
before they are written, so the sum of the target durations can never exceed
the uploaded voiceover length. This is what keeps "final video duration ==
voiceover audio duration" true — alignment models occasionally report end
times past the real audio, and unclamped targets used to stretch every clip
to an inflated length (real-media QA job 6b2c0929-...: 797.8s of video from
a 522s audio).
"""

import json
import logging
import tempfile
from pathlib import Path

from google import genai
from google.genai import types as genai_types

from pipeline import config, job_config, job_logging, key_store, lang_files, video_ingest
from pipeline.subtitle_extract import _extract_json, call_with_rotation
from pipeline.voiceover_auto import _probe_audio_duration, _run
from pipeline.voiceover_unify import VoiceoverAlignmentError, _clamp_timestamps_to_audio
from pipeline.whisper_align import (
    engine_allows_whisper,
    last_speech_end,
    match_words_to_entries as _match_words_to_entries,
    transcribe_words as _transcribe_words,
)

logger = logging.getLogger(__name__)

ALLOWED_AUDIO_EXTS = {".mp3", ".wav", ".m4a"}

ALIGNMENT_PROMPT = (
    "You are given one complete Hindi voiceover recording. A list of subtitle "
    "lines (with serial numbers, in order) is provided below. For every line, "
    "determine the start and end time (in seconds) within the audio where that "
    "line is spoken. Every line must appear exactly once. Respond with ONLY "
    'JSON, no commentary, in this exact structure: {"alignments": '
    '[{"serial": 1, "start_sec": 0.0, "end_sec": 3.2}]}'
)


class UnsupportedAudioError(Exception):
    """Raised when the uploaded voiceover file type is not supported."""


def _convert_to_wav(src_path, out_path):
    """Normalize any accepted upload to mono PCM wav (same format as D2)."""
    _run(
        [
            "ffmpeg", "-y", "-i", str(src_path),
            "-ar", str(config.TTS_SAMPLE_RATE), "-ac", "1",
            "-c:a", "pcm_s16le", str(out_path),
        ],
        120,
    )


def save_uploaded_voiceover(job_id, audio_bytes, filename, upload_root=None):
    """Save a user voiceover upload as ``uploads/<job_id>/voiceover_hi.wav``.

    Raises FileNotFoundError for an unknown job, UnsupportedAudioError for a
    bad extension, and RuntimeError when ffmpeg cannot read the audio.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job not found: {job_id}")

    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTS:
        raise UnsupportedAudioError(
            f"Unsupported audio type '{ext or '(none)'}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_AUDIO_EXTS))}"
        )

    job_logger = job_logging.get_job_logger(job_id, upload_root)
    lang = lang_files.target_lang(job_id, upload_root)
    out_path = job_dir / lang_files.voiceover_audio(lang)
    with tempfile.TemporaryDirectory(dir=str(job_dir)) as tmpdir:
        tmp_path = Path(tmpdir) / f"upload{ext}"
        tmp_path.write_bytes(audio_bytes)
        _convert_to_wav(tmp_path, out_path)
    job_logger.info("job %s: voiceover saved to %s", job_id, out_path)
    return out_path


def _call_gemini_align(key, audio_path, entries):
    """Send one audio file + subtitle list to Gemini, return per-serial spans."""
    client = genai.Client(api_key=key)
    uploaded = client.files.upload(file=str(audio_path))
    lines = json.dumps(
        [
            {"serial": e.get("serial"), "text": lang_files.entry_text(e)}
            for e in entries
        ],
        ensure_ascii=False,
    )
    prompt = ALIGNMENT_PROMPT + "\n\nSUBTITLES:\n" + lines
    response = client.models.generate_content(
        model=config.ALIGNMENT_MODEL,
        contents=[
            genai_types.Part.from_uri(
                file_uri=uploaded.uri, mime_type="audio/wav"
            ),
            prompt,
        ],
    )
    data = _extract_json(response.text)
    raw = data.get("alignments", []) if isinstance(data, dict) else []
    if not isinstance(raw, list):
        raise ValueError("malformed alignment payload")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            serial = int(item.get("serial"))
            start_sec = float(item.get("start_sec"))
            end_sec = float(item.get("end_sec"))
        except (TypeError, ValueError):
            continue
        result.append({"serial": serial, "start_sec": start_sec, "end_sec": end_sec})
    if not result:
        raise ValueError("empty alignment payload")
    return result


def _gemini_align(keys, entries, audio_path, logger_=None):
    """Try Gemini across active keys; None when it fails or is incomplete."""
    log = logger_ or logger
    rotation = 0
    alignments, _, _ = call_with_rotation(
        keys, rotation, _call_gemini_align, audio_path, entries, logger_=log
    )
    if alignments is None:
        return None

    by_serial = {}
    for item in alignments:
        by_serial[int(item["serial"])] = item
    expected = [e.get("serial") for e in entries]
    if not all(serial in by_serial for serial in expected):
        log.error("Gemini alignment incomplete (missing serials); falling back")
        return None

    return [
        {
            "serial": serial,
            "start_sec": round(float(by_serial[serial]["start_sec"]), 3),
            "end_sec": round(float(by_serial[serial]["end_sec"]), 3),
            "alignment_fallback": False,
            "alignment_source": "gemini",
        }
        for serial in expected
    ]


def _gemini_align_secondary(keys, entries, audio_path, logger_=None):
    """Gemini audio alignment for a SUBSET of serials (whisper-primary D3).

    The primary Whisper pass matched most serials; Gemini is asked only for the
    ones it could not match, so it can never override a Whisper match. Partial
    results are accepted (unlike :func:`_gemini_align` which demands every
    serial): the caller additionally bounds each accepted item by the
    Whisper-detected speech tail.

    Returns the raw ``[{"serial", "start_sec", "end_sec"}]`` list (possibly
    partial), or ``None`` when Gemini fails entirely.
    """
    log = logger_ or logger
    rotation = 0
    alignments, _, _ = call_with_rotation(
        keys, rotation, _call_gemini_align, audio_path, entries, logger_=log
    )
    if not alignments:
        return None
    items = []
    for item in alignments:
        try:
            serial = int(item["serial"])
            start_sec = float(item["start_sec"])
            end_sec = float(item["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        items.append({"serial": serial, "start_sec": start_sec, "end_sec": end_sec})
    return items or None


def _apply_gemini_assisted(timestamps, gemini_items, speech_end, tolerance):
    """Apply bounded Gemini secondary results onto unmatched serials.

    Only serials whose ``end_sec`` is within ``speech_end + tolerance`` are
    accepted (``alignment_source="gemini_assisted"``) — Gemini can never place
    audio past what Whisper actually detected. Serial order is preserved.
    Returns ``(timestamps, applied_serials)``.
    """
    by_serial = {entry["serial"]: entry for entry in timestamps}
    applied = []
    for item in gemini_items or []:
        entry = by_serial.get(item["serial"])
        if entry is None or entry["alignment_source"] != "equal_split":
            continue
        if item["end_sec"] > speech_end + tolerance:
            continue
        entry["start_sec"] = round(item["start_sec"], 3)
        entry["end_sec"] = round(item["end_sec"], 3)
        entry["alignment_source"] = "gemini_assisted"
        entry["alignment_fallback"] = True
        applied.append(entry["serial"])
    ordered = [by_serial[entry["serial"]] for entry in timestamps]
    return ordered, applied


def _finalize_timestamps(entries, matches, total_sec):
    """Build the final timestamp list, filling unmatched lines equally.

    Matched serials keep their ``(start, end)``; runs of unmatched serials are
    equal-split between the previous matched end and the next matched start
    (or the audio bounds). Order is always preserved.

    Whisper is the primary authority (F8): whisper-matched serials are
    ``alignment_source="whisper"`` with ``alignment_fallback=False``; only
    equal-split placeholders are fallbacks (and later the bounded Gemini
    secondary results, ``"gemini_assisted"``).
    """
    n = len(entries)
    starts = [None] * n
    ends = [None] * n
    sources = [None] * n
    for i, entry in enumerate(entries):
        match = matches.get(entry["serial"])
        if match is not None:
            starts[i] = float(match["start_sec"])
            ends[i] = float(match["end_sec"])
            sources[i] = "whisper"

    i = 0
    while i < n:
        if starts[i] is not None:
            i += 1
            continue
        j = i
        while j < n and starts[j] is None:
            j += 1
        left = ends[i - 1] if i > 0 else 0.0
        right = starts[j] if j < n else float(total_sec)
        span = max(float(right) - float(left), 0.0)
        count = j - i
        seg = span / count if count else 0.0
        for k in range(i, j):
            starts[k] = left + (k - i) * seg
            ends[k] = starts[k] + seg
            sources[k] = "equal_split"
        i = j

    out = []
    for i, entry in enumerate(entries):
        out.append(
            {
                "serial": entry["serial"],
                "start_sec": round(starts[i], 3),
                "end_sec": round(ends[i], 3),
                "alignment_fallback": sources[i] != "whisper",
                "alignment_source": sources[i],
            }
        )
    return out


def _status_from_sources(sources):
    """Map the set of per-serial alignment sources onto (status, source).

    F8 taxonomy:
    - all Whisper -> "ok" (Whisper, the primary authority, matched everything)
    - all Gemini -> "ok" (pure-Gemini fallback: Whisper was unavailable)
    - any gemini_assisted -> "gemini_assisted" (Whisper primary, bounded Gemini
      secondary resolved some unmatched serials)
    - all equal_split -> "equal_split"
    - Whisper + equal_split -> "whisper" (Whisper matched some but Gemini did
      not help; the rest got placeholder timing)
    """
    if sources == {"whisper"} or sources == {"gemini"}:
        return "ok", next(iter(sources))
    if sources == {"equal_split"}:
        return "equal_split", "equal_split"
    if "gemini_assisted" in sources:
        return "gemini_assisted", "whisper"
    return "whisper", "whisper"


def _resolve_whisper_language(target_lang, logger_=None):
    """Whisper language hint for a job's ``target_lang`` (F12f, Part D).

    ``target_lang`` codes (``hi`` / ``bn`` / ``en``) already match Whisper's
    ISO-639-1 language codes, so the value passes through unchanged and a
    ``hi`` job gets exactly the pre-F12f ``language="hi"`` hint. A missing or
    unsupported code falls back to the default ``"hi"`` instead of passing
    ``None``/empty to Whisper, and logs the fallback so it is visible in the
    job log.
    """
    log = logger_ or logger
    if target_lang in job_config.ALLOWED_TARGET_LANGS:
        return target_lang
    log.warning(
        "whisper alignment: unsupported target_lang %r; "
        "falling back to %r",
        target_lang, job_config.DEFAULT_TARGET_LANG,
    )
    return job_config.DEFAULT_TARGET_LANG


def align_uploaded_voiceover(job_id, upload_root=None):
    """Align ``text_hi`` lines to the uploaded voiceover. Never raises on
    Gemini/Whisper failures.

    Raises FileNotFoundError when the job has no ``voiceover_hi.wav`` or no
    ``subtitles_hi.json``. Writes ``timestamps_hi_upload.json``.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    lang = lang_files.target_lang(job_id, upload_root)
    audio_name = lang_files.voiceover_audio(lang)
    audio_path = job_dir / audio_name
    if not audio_path.exists():
        raise FileNotFoundError(f"no {audio_name} for job {job_id}")

    sub_name = lang_files.subtitles_json(lang)
    in_path = job_dir / sub_name
    if not in_path.exists():
        raise FileNotFoundError(f"no {sub_name} for job {job_id}")

    job_logger = job_logging.get_job_logger(job_id, upload_root)
    entries = json.loads(in_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"malformed {sub_name} for job {job_id}")

    timestamps_path = job_dir / lang_files.timestamps_upload(lang)
    if not entries:
        timestamps_path.write_text("[]", encoding="utf-8")
        return {
            "job_id": job_id,
            "status": "ok",
            "alignment_source": "gemini",
            "fallback_used": False,
            "fallback_serials": [],
            "entries_count": 0,
            "total_sec": 0.0,
            "target_total_sec": 0.0,
            "clamped_serials": [],
            "warnings": [],
            "voiceover_path": str(audio_path),
            "timestamps_path": str(timestamps_path),
        }

    try:
        total_sec = _probe_audio_duration(audio_path)
    except Exception as exc:  # noqa: BLE001 - fallback must survive
        job_logger.error("job %s: cannot probe voiceover duration: %s", job_id, exc)
        total_sec = 0.0

    if total_sec <= 0:
        # No measurable audio content -> no segment can be aligned to real
        # audio. This is a genuine per-segment alignment failure (not a
        # total-duration mismatch, which is normal translation drift), so it
        # blocks and tells the user what to do.
        raise VoiceoverAlignmentError(
            f"voiceover alignment failed for job {job_id}: the uploaded audio "
            "has no measurable content. Re-upload a valid audio file."
        )

    keys = key_store.get_active_keys()

    # Whisper is the primary timing authority (F8). When Whisper is
    # unavailable/fails entirely, we fall back to today's pure-Gemini flow.
    # F9: a gemini_only job skips Whisper even when it is installed.
    words = (
        _transcribe_words(
            audio_path,
            language=_resolve_whisper_language(lang, logger_=job_logger),
            model=config.WHISPER_MODEL_HI,
            logger_=job_logger,
        )
        if engine_allows_whisper(job_id, upload_root)
        else None
    )

    gemini_assisted_serials = []
    if words is None:
        timestamps = _gemini_align(keys, entries, audio_path, logger_=job_logger)
        if timestamps is None:
            job_logger.error("job %s: Gemini alignment failed; using equal split", job_id)
            timestamps = _finalize_timestamps(entries, {}, total_sec)
    else:
        matches = _match_words_to_entries(words, entries)
        job_logger.info("job %s: whisper primary matched %d/%d lines",
                        job_id, len(matches), len(entries))
        timestamps = _finalize_timestamps(entries, matches, total_sec)
        unmatched = [e for e in entries if e["serial"] not in matches]
        if unmatched and keys:
            speech_end = last_speech_end(words)
            gemini_items = _gemini_align_secondary(
                keys, unmatched, audio_path, logger_=job_logger
            )
            if gemini_items:
                timestamps, gemini_assisted_serials = _apply_gemini_assisted(
                    timestamps, gemini_items, speech_end,
                    config.WHISPER_TAIL_TOLERANCE_SEC,
                )
                if gemini_assisted_serials:
                    job_logger.info(
                        "job %s: gemini secondary resolved %d unmatched serial(s)",
                        job_id, len(gemini_assisted_serials),
                    )

    # Duration-drift guard (E9): alignment models can return end times past
    # the real audio length, so the per-serial target durations would sum to
    # more than the uploaded voiceover and E2 would stretch every clip to an
    # inflated target (real-media QA job 6b2c0929-...: 797.8s video from a
    # 522s audio). Clamp to the probed audio duration so the target durations
    # always tile inside the audio and the final video can never be longer
    # than the voiceover.
    timestamps, clamped_serials = _clamp_timestamps_to_audio(timestamps, total_sec)

    sources = {entry["alignment_source"] for entry in timestamps}
    status, source = _status_from_sources(sources)

    fallback_serials = [
        entry["serial"] for entry in timestamps if entry["alignment_fallback"]
    ]

    warnings = []
    if clamped_serials:
        warnings.append(
            f"{len(clamped_serials)} aligned segment(s) ran past the uploaded "
            "audio's real duration and were clamped to it, so the final video "
            "can never exceed the voiceover length."
        )
    if status == "equal_split":
        warnings.append(
            "Uploaded audio could not be matched to any subtitle line; "
            "placeholder (equal-split) timings were used for every segment. "
            "The dubbing will still render, but line timing may be off."
        )
    elif status == "whisper":
        warnings.append(
            f"{len(fallback_serials)} line(s) could not be matched to the "
            "uploaded audio and got placeholder timing. The dubbing will "
            "still render, but those segments may be mis-timed."
        )
    elif status == "gemini_assisted":
        warnings.append(
            f"{len(fallback_serials)} line(s) could not be matched by Whisper; "
            f"{len(gemini_assisted_serials)} were resolved by Gemini within the "
            "detected speech range and the rest got placeholder timing."
        )

    timestamps_path.write_text(
        json.dumps(timestamps, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    job_logger.info("job %s: alignment finished (status=%s)", job_id, status)

    return {
        "job_id": job_id,
        "status": status,
        "alignment_source": source,
        "fallback_used": status != "ok",
        "fallback_serials": fallback_serials,
        "entries_count": len(entries),
        "total_sec": round(total_sec, 3),
        "target_total_sec": round(
            sum(e["end_sec"] - e["start_sec"] for e in timestamps), 3
        ),
        "clamped_serials": clamped_serials,
        "warnings": warnings,
        "voiceover_path": str(audio_path),
        "timestamps_path": str(timestamps_path),
    }
