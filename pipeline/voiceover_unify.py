"""Voiceover source unification (D1 + D4).

Records how the Hindi voiceover will be produced for a job: either by the
system itself (``"auto_tts"``, Gemini TTS) or from an audio file the user
created themselves / with another AI (``"user_upload"``).

The choice is persisted to ``uploads/<job_id>/voice_source_choice.json`` and
is consumed by D2 (auto-TTS path) and D3 (user-upload path). D4's
``unify_voiceover_timestamps`` merges either path into one common format,
``uploads/<job_id>/timestamps_hi_final.json``, so later chunks can be
mode-agnostic.
"""

import json
import logging
from pathlib import Path

from pipeline import video_ingest

logger = logging.getLogger(__name__)

ALLOWED_MODES = ("auto_tts", "user_upload")


class InvalidVoiceSourceError(Exception):
    """Raised when the mode is not one of ALLOWED_MODES."""


class VoiceoverAlignmentError(Exception):
    """Raised when the voiceover could not be aligned per subtitle segment.

    This is the real accuracy check for the user_upload path: every subtitle
    segment must end up with a voiceover timestamp. A subtitle serial with no
    matching timestamp means no audio was found/aligned for that segment, which
    is a hard failure (unlike a total-duration mismatch, which is normal
    translation drift and never blocks).
    """


def _choice_path(job_id, upload_root):
    return Path(upload_root) / job_id / "voice_source_choice.json"


def set_voice_source(job_id, mode, upload_root=None):
    """Save the voice source mode for a job. Returns the saved dict."""
    if mode not in ALLOWED_MODES:
        raise InvalidVoiceSourceError(
            f"invalid voice source mode: {mode!r} (allowed: {', '.join(ALLOWED_MODES)})"
        )
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job not found: {job_id}")
    data = {"job_id": job_id, "mode": mode}
    _choice_path(job_id, upload_root).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("voice source for job %s set to %s", job_id, mode)
    return data


def get_voice_source(job_id, upload_root=None):
    """Return the saved mode for a job, or None if not set yet."""
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    path = _choice_path(job_id, upload_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("mode") if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


# D2/D3 timestamp files, keyed by the voice source mode. Both carry the same
# shape (serial, start_sec, end_sec) with different per-path flag names.
SOURCE_TIMESTAMPS_FILES = {
    "auto_tts": "timestamps_hi_auto.json",
    "user_upload": "timestamps_hi_upload.json",
}


def _load_timestamps_list(path):
    if not path.exists():
        raise FileNotFoundError(f"missing file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"expected a list in {path}")
    return data


def _map_timestamp_entry(entry):
    """Map a D2/D3 entry onto the common unified schema.

    The distinct per-path flags collapse into one ``flagged`` boolean plus a
    ``flag_reason`` that is ``"tts_failed"`` or ``"alignment_fallback"`` (or
    None when the entry is clean). ``tts_failed`` wins if both were somehow set.
    """
    try:
        serial = int(entry.get("serial"))
        start_sec = float(entry.get("start_sec", 0.0))
        end_sec = float(entry.get("end_sec", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed timestamp entry: {entry!r}") from exc

    tts_failed = bool(entry.get("tts_failed", False))
    alignment_fallback = bool(entry.get("alignment_fallback", False))
    if tts_failed:
        flag_reason = "tts_failed"
    elif alignment_fallback:
        flag_reason = "alignment_fallback"
    else:
        flag_reason = None

    return {
        "serial": serial,
        "start_sec": round(start_sec, 3),
        "end_sec": round(end_sec, 3),
        "flagged": tts_failed or alignment_fallback,
        "flag_reason": flag_reason,
    }


def _clamp_consecutive_overlaps(entries):
    """Clamp consecutive overlaps deterministically (B2-style).

    Each entry's start is pulled up to the previous entry's end when it
    overlaps; a collapsed range (end < start) is clamped to zero length.
    Returns ``(entries, clamped_serials)``.
    """
    out = []
    clamped_serials = []
    prev_end = None
    for entry in entries:
        start = float(entry["start_sec"])
        end = float(entry["end_sec"])
        if prev_end is not None and start < prev_end:
            logger.warning(
                "voiceover serial %d overlap: start %.3fs clamped to previous end %.3fs",
                entry["serial"], start, prev_end,
            )
            start = prev_end
            clamped_serials.append(entry["serial"])
        if end < start:
            end = start
        out.append(
            {
                **entry,
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
            }
        )
        prev_end = out[-1]["end_sec"]
    return out, clamped_serials


def _missing_serials(job_dir, final_entries):
    """Subtitle serials with no matching voiceover timestamp, if known.

    Reads ``subtitles_hi.json`` when present (both D2 and D3 align to its
    serials) and returns the sorted list of serials that exist there but have
    no entry in the unified timestamps. When the subtitle file is missing or
    unreadable, returns ``[]`` so this check never breaks a caller that only
    works with timestamps (e.g. the direct D4 unit fixtures).
    """
    subs_path = job_dir / "subtitles_hi.json"
    if not subs_path.exists():
        return []
    try:
        subs = json.loads(subs_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(subs, list):
        return []

    sub_serials = set()
    for entry in subs:
        try:
            sub_serials.add(int(entry.get("serial")))
        except (TypeError, ValueError):
            continue
    final_serials = set()
    for entry in final_entries:
        try:
            final_serials.add(int(entry["serial"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(sub_serials - final_serials)


def unify_voiceover_timestamps(job_id, upload_root=None):
    """Merge the chosen voiceover path into one common format (D4).

    Reads ``voice_source_choice.json``, picks ``timestamps_hi_auto.json``
    (D2) or ``timestamps_hi_upload.json`` (D3), maps both onto one common
    schema with a unified ``flagged`` / ``flag_reason``, deterministically
    clamps any consecutive overlap (B2-style, logged) and writes
    ``uploads/<job_id>/timestamps_hi_final.json``.

    ``voiceover_hi.wav`` is shared by D2 and D3 at the same path; its presence
    is verified so later chunks can use it mode-agnostically.

    Per-segment alignment validation: when ``subtitles_hi.json`` is present,
    every subtitle serial must have a matching unified timestamp. A subtitle
    segment with no voiceover timestamp means no audio was aligned for it,
    which raises :class:`VoiceoverAlignmentError` (the real user_upload
    correctness check — total-duration mismatches never block).

    Raises FileNotFoundError when the job, the voice source choice, the chosen
    timestamps file or the voiceover audio is missing; ValueError on malformed
    timestamps; VoiceoverAlignmentError when a subtitle segment has no aligned
    voiceover timestamp.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job not found: {job_id}")

    mode = get_voice_source(job_id, upload_root)
    if mode not in ALLOWED_MODES:
        raise FileNotFoundError(
            f"no valid voice source choice for job {job_id} "
            f"(choose one via /voiceover/{job_id}/choose first)"
        )

    source_name = SOURCE_TIMESTAMPS_FILES[mode]
    source_path = job_dir / source_name
    if not source_path.exists():
        phase = "auto TTS (D2)" if mode == "auto_tts" else "uploaded voiceover alignment (D3)"
        raise FileNotFoundError(f"no {source_name} for job {job_id} ({phase} not done yet?)")

    audio_path = job_dir / "voiceover_hi.wav"
    if not audio_path.exists():
        raise FileNotFoundError(f"no voiceover_hi.wav for job {job_id}")

    entries = _load_timestamps_list(source_path)
    entries.sort(key=lambda entry: int(entry.get("serial", 0)))
    mapped = [_map_timestamp_entry(entry) for entry in entries]
    final, clamped_serials = _clamp_consecutive_overlaps(mapped)

    missing = _missing_serials(job_dir, final)
    if missing:
        logger.error(
            "job %s: %d subtitle segment(s) have no aligned voiceover timestamp: %s",
            job_id, len(missing), missing,
        )
        raise VoiceoverAlignmentError(
            f"voiceover alignment failed for job {job_id}: no audio timing "
            f"found for subtitle segment(s) {missing}. Re-upload the audio "
            "or re-run the alignment before continuing."
        )

    out_path = job_dir / "timestamps_hi_final.json"
    out_path.write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "job %s: unified %d voiceover timestamps from %s (%d flagged, %d clamped)",
        job_id, len(final), source_name,
        sum(1 for e in final if e["flagged"]), len(clamped_serials),
    )

    return {
        "job_id": job_id,
        "mode": mode,
        "source_timestamps": source_name,
        "entries_count": len(final),
        "flagged_count": sum(1 for e in final if e["flagged"]),
        "clamped_serials": clamped_serials,
        "missing_serials": missing,
        "voiceover_path": str(audio_path),
        "timestamps_path": str(out_path),
        "timestamps": final,
    }
