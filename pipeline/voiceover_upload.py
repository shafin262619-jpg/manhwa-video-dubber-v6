"""User-uploaded Hindi voiceover alignment (D3).

The user records one complete Hindi voiceover file (every ``subtitles_hi.json``
line read in serial order). The file is saved as ``uploads/<job_id>/voiceover_hi.wav``
and ``align_uploaded_voiceover`` finds, for every serial, the second-range in the
audio where that line is spoken, writing ``uploads/<job_id>/timestamps_hi_upload.json``
in the same schema D2 uses: ``[{"serial", "start_sec", "end_sec",
"alignment_fallback"}]``.

Alignment strategy (resilience ladder, mirrors the "Gemini fail -> Whisper
fallback" pattern):

1. Gemini audio-understanding (audio + the subtitle list) returns the start/end
   seconds per serial. Used only when every serial is present and numeric.
2. On Gemini failure / malformed response: the whole audio is transcribed with
   local Whisper (word timestamps) and Whisper's text is matched to the subtitle
   lines sequentially with fuzzy matching. Unmatched lines are filled between
   their matched neighbours (equal-split of the gap).
3. If Whisper is also unavailable / matches nothing: every line gets an
   equal-split of the total audio duration.

Every line whose timing did not come from the primary Gemini pass is flagged
``alignment_fallback: true`` (plus an ``alignment_source`` field for clarity).
``align_uploaded_voiceover`` never raises on Gemini/Whisper failures — the
only alignment failure it treats as blocking is an audio file with no
measurable content (``total_sec <= 0``), which raises
:class:`~pipeline.voiceover_unify.VoiceoverAlignmentError`.
"""

import json
import logging
import re
import tempfile
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from google import genai
from google.genai import types as genai_types

from pipeline import config, job_logging, key_store, video_ingest
from pipeline.subtitle_extract import _extract_json, call_with_rotation
from pipeline.voiceover_auto import _probe_audio_duration, _run
from pipeline.voiceover_unify import VoiceoverAlignmentError

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


def _norm_text(text):
    """Normalize text for fuzzy matching: strip punctuation, keep letters."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"[^\w\u0900-\u097F]+", "", text, flags=re.UNICODE)
    return text.lower()


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
    out_path = job_dir / "voiceover_hi.wav"
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
        [{"serial": e.get("serial"), "text_hi": e.get("text_hi")} for e in entries],
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


def _transcribe_words(audio_path, logger_=None):
    """Transcribe the audio with local Whisper; return word-level timestamps.

    Returns None when whisper is unavailable or the transcription fails.
    """
    log = logger_ or logger
    try:
        import whisper  # lazy: heavy optional dependency
    except ImportError as exc:
        log.error("whisper not installed; skipping whisper fallback: %s", exc)
        return None
    try:
        model = whisper.load_model(config.WHISPER_MODEL)
        result = model.transcribe(str(audio_path), word_timestamps=True)
    except Exception as exc:  # noqa: BLE001 - resilience: fallback must survive
        log.error("whisper transcription failed: %s", exc)
        return None

    words = []
    for segment in result.get("segments", []) or []:
        for word in segment.get("words", []) or []:
            try:
                words.append(
                    {
                        "word": str(word.get("word", "")),
                        "start": float(word.get("start", 0.0)),
                        "end": float(word.get("end", 0.0)),
                    }
                )
            except (TypeError, ValueError):
                continue
    return words


def _match_words_to_entries(words, entries):
    """Sequential fuzzy match of subtitle lines onto the whisper word stream.

    Returns ``{serial: {"start_sec", "end_sec"}}`` for the lines that matched
    above ``WHISPER_MATCH_MIN_RATIO``; lines without usable text are skipped.
    """
    tokens = [w for w in words if _norm_text(w.get("word"))]
    if not tokens:
        return {}

    n = len(tokens)
    cursor = 0
    matches = {}
    for entry in entries:
        target = _norm_text(entry.get("text_hi"))
        if not target:
            continue
        best = None
        for start_idx in range(cursor, n):
            acc = ""
            best_ratio = 0.0
            best_end = start_idx
            for end_idx in range(start_idx, n):
                acc += _norm_text(tokens[end_idx].get("word"))
                if len(acc) > len(target) * 1.6 + 4:
                    break
                ratio = SequenceMatcher(None, acc, target).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_end = end_idx
                if best_ratio >= 0.95:
                    break
            if best_ratio >= config.WHISPER_MATCH_MIN_RATIO:
                best = (start_idx, best_end)
                break
        if best is not None:
            start_idx, end_idx = best
            matches[entry["serial"]] = {
                "start_sec": float(tokens[start_idx]["start"]),
                "end_sec": float(tokens[end_idx]["end"]),
            }
            cursor = end_idx + 1
    return matches


def _finalize_timestamps(entries, matches, total_sec):
    """Build the final timestamp list, filling unmatched lines equally.

    Matched serials keep their ``(start, end)``; runs of unmatched serials are
    equal-split between the previous matched end and the next matched start
    (or the audio bounds). Order is always preserved.
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
                "alignment_fallback": sources[i] != "gemini",
                "alignment_source": sources[i],
            }
        )
    return out


def align_uploaded_voiceover(job_id, upload_root=None):
    """Align ``text_hi`` lines to the uploaded voiceover. Never raises on
    Gemini/Whisper failures.

    Raises FileNotFoundError when the job has no ``voiceover_hi.wav`` or no
    ``subtitles_hi.json``. Writes ``timestamps_hi_upload.json``.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    audio_path = job_dir / "voiceover_hi.wav"
    if not audio_path.exists():
        raise FileNotFoundError(f"no voiceover_hi.wav for job {job_id}")

    in_path = job_dir / "subtitles_hi.json"
    if not in_path.exists():
        raise FileNotFoundError(f"no subtitles_hi.json for job {job_id}")

    job_logger = job_logging.get_job_logger(job_id, upload_root)
    entries = json.loads(in_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"malformed subtitles_hi.json for job {job_id}")

    timestamps_path = job_dir / "timestamps_hi_upload.json"
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
    timestamps = _gemini_align(keys, entries, audio_path, logger_=job_logger)

    if timestamps is None:
        words = _transcribe_words(audio_path, logger_=job_logger)
        matches = _match_words_to_entries(words, entries) if words else {}
        if not matches:
            job_logger.error("job %s: whisper fallback unusable; using equal split", job_id)
            timestamps = _finalize_timestamps(entries, {}, total_sec)
        else:
            job_logger.info("job %s: whisper fallback matched %d/%d lines",
                            job_id, len(matches), len(entries))
            timestamps = _finalize_timestamps(entries, matches, total_sec)

    sources = {entry["alignment_source"] for entry in timestamps}
    if sources == {"gemini"}:
        status, source = "ok", "gemini"
    elif sources == {"equal_split"}:
        status, source = "equal_split", "equal_split"
    else:
        status, source = "whisper", "whisper"

    fallback_serials = [
        entry["serial"] for entry in timestamps if entry["alignment_fallback"]
    ]

    warnings = []
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
        "warnings": warnings,
        "voiceover_path": str(audio_path),
        "timestamps_path": str(timestamps_path),
    }
