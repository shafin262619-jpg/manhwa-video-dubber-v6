"""Chinese subtitle extraction via Gemini video understanding + Whisper timing (F8).

For each job, videos shorter than ``LONG_VIDEO_CHUNK_THRESHOLD_SEC`` are sent to
Gemini in one shot. Longer videos are split into overlapping segments (see
``SUBTITLE_OVERLAP_SEC``), each segment is sent separately with its absolute
start offset added back to the timestamps, then overlap duplicates are
de-duplicated into one continuous list.

Whisper is the primary timing authority (F1-F3): after each chunk's Gemini
extraction, the chunk audio is transcribed with local Whisper and every
Whisper segment becomes a subtitle entry (``text_source`` is ``"gemini_cleaned"``
when Gemini's text matched the segment, ``"whisper_raw"`` otherwise). Gemini
lines that overlap no Whisper segment are dropped and counted in
``gemini_hallucinated_dropped``. When Whisper is unavailable or hears no
speech, today's pure-Gemini output is returned unchanged.

Resilience rules:

- Gemini keys are taken from ``key_store.get_active_keys()`` and rotated
  round-robin via :func:`call_with_rotation`, a thin wrapper over
  ``gemini_rotation.call_with_rotation_v2``. Transient / unknown errors are
  classified ``rotatable`` and rotate to the next key; content-safety blocks
  are ``non_rotatable`` and stop immediately. A whole job run can share one
  ``gemini_rotation.CallBudget`` (see ``config.MAX_API_CALLS_PER_JOB``).
- Content-blocked responses (``prompt_feedback.block_reason`` /
  ``FinishReason.SAFETY``) are key-independent: they are logged distinctly as
  ``content_blocked`` and never retried or rotated.
- If every key fails for a segment (or the response is malformed JSON), that
  segment is flagged in ``failed_segments`` with the last exception message
  recorded under ``errors``, logged, and processing continues.
- ``extract_subtitles`` never raises on Gemini/Whisper/ffmpeg failures. It
  returns a result with status ``ok`` / ``partial`` / ``extraction_failed``
  and writes ``uploads/<job_id>/subtitles_zh_raw.json``.
"""

import json
import logging
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

from google import genai
from google.genai import types as genai_types

from pipeline import config, job_logging, job_status, key_store, video_ingest
from pipeline.gemini_rotation import (
    AllKeysExhausted,
    CallBudgetExceeded,
    call_with_rotation_v2,
)
from pipeline.whisper_align import (
    engine_allows_whisper,
    overlap_ratio,
    transcribe_segments,
)

logger = logging.getLogger(__name__)

SUBTITLE_EXTRACT_PROMPT = (
    "Extract every subtitle and on-screen text shown in this video, verbatim "
    "and in chronological order, with each one's accurate start and end "
    "seconds. Respond with ONLY JSON, no commentary, in this exact structure: "
    '{"subtitles": [{"text": "...", "start_sec": 0.0, "end_sec": 3.2}]}'
)

# ``FinishReason`` values that mean the request was blocked by safety/content
# policy rather than failing transiently (Task 5).
_CONTENT_BLOCK_FINISH_REASONS = (
    "SAFETY",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "RECITATION",
)

# Per-process cache of uploaded video files so a rate-limit retry reuses the
# already-uploaded file instead of re-uploading it (Task 3). Keyed by
# (api_key, path, size, mtime_ns): a file re-uploaded for a new job is a new
# signature, and one key never references another key's upload.
_UPLOAD_CACHE = {}


class ContentBlockedError(RuntimeError):
    """Raised when Gemini returns a content-blocked response.

    Carries the blocking reason (e.g. ``SAFETY`` / ``BLOCKLIST``) so the
    retry/rotation logic can treat it as key-independent: no retry, no rotate.
    """

    def __init__(self, reason, message=None):
        self.reason = reason
        self.message = message
        detail = f" ({message})" if message else ""
        super().__init__(f"content blocked ({reason}){detail}")


def _block_reason_from_response(response):
    """Extract ``prompt_feedback.block_reason`` as a dict or None."""
    try:
        feedback = response.prompt_feedback
    except Exception:  # noqa: BLE001 - attribute may not exist on fakes
        feedback = None
    if feedback is None:
        return None
    reason = getattr(feedback, "block_reason", None)
    if reason is None:
        return None
    name = getattr(reason, "name", None) or str(reason)
    message = getattr(feedback, "block_reason_message", None)
    return {"reason": name, "message": message or None}


def _is_content_blocked(exc, response=None):
    """Return a block-reason dict when Gemini blocked the request, else None."""
    if response is not None:
        reason = _block_reason_from_response(response)
        if reason is not None:
            return reason
        for candidate in getattr(response, "candidates", None) or []:
            finish_reason = getattr(candidate, "finish_reason", None)
            if finish_reason is None:
                continue
            name = getattr(finish_reason, "name", None) or str(finish_reason)
            if name in _CONTENT_BLOCK_FINISH_REASONS:
                return {"reason": name, "message": None}
    text = str(exc or "").lower()
    markers = (
        "blocked",
        "block_reason",
        "prompt_feedback",
        "promptfeedback",
        "prohibited_content",
        "content_safety",
        "safety_ratings",
        "finish_reason.safety",
        "blocklist",
    )
    if any(marker in text for marker in markers):
        return {"reason": "content_blocked", "message": str(exc)}
    return None


def _normalize(text):
    return re.sub(r"\s+", "", str(text or "")).strip()


def _load_job_meta(job_dir):
    path = job_dir / "job_meta.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _run_ffmpeg(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"ffmpeg failed: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {result.stderr.strip()}")


def _segment_video(job_dir, source, duration_sec):
    """Split a long video into overlapping segments via ffmpeg."""
    overlap = config.SUBTITLE_OVERLAP_SEC
    seg_len = config.LONG_VIDEO_CHUNK_THRESHOLD_SEC
    seg_dir = job_dir / "segments"
    seg_dir.mkdir(exist_ok=True)

    segments = []
    idx = 0
    start = 0.0
    while start < duration_sec - 1e-6:
        end = min(start + seg_len, duration_sec)
        out = seg_dir / f"seg_{idx:03d}.mp4"
        _run_ffmpeg(
            [
                "ffmpeg", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                "-i", str(source), "-c", "copy", str(out),
            ]
        )
        segments.append({"index": idx, "start": start, "end": end, "path": out})
        idx += 1
        if end >= duration_sec - 1e-6:
            break
        start = max(end - overlap, start + 0.5)
    return segments


def _extract_json(text):
    """Parse a Gemini text response into a dict, tolerating fences/noise."""
    text = str(text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("malformed JSON from Gemini")


def _parse_subtitles(text, offset_sec):
    """Parse Gemini output into absolute subtitle dicts (offset applied)."""
    data = _extract_json(text)
    raw = data.get("subtitles", []) if isinstance(data, dict) else []
    if not isinstance(raw, list):
        raise ValueError("malformed subtitles payload")
    subtitles = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start_sec", 0.0)) + offset_sec
            end = float(item.get("end_sec", 0.0)) + offset_sec
        except (TypeError, ValueError):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        subtitles.append(
            {"text": text, "start_sec": start, "end_sec": end}
        )
    return subtitles


def _upload_signature(video_path):
    try:
        stat = video_path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None, None


def _get_or_upload(client, key, video_path):
    """Upload a segment once and reuse the uploaded file on retries (Task 3)."""
    signature = (key, str(video_path)) + tuple(_upload_signature(video_path))
    uploaded = _UPLOAD_CACHE.get(signature)
    if uploaded is None:
        uploaded = client.files.upload(file=str(video_path))
        _UPLOAD_CACHE[signature] = uploaded
    return uploaded


def _call_gemini(key, prompt, video_path, offset_sec):
    """Send one video (or segment) to Gemini and return parsed subtitles."""
    client = genai.Client(api_key=key)
    uploaded = _get_or_upload(client, key, video_path)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[
            genai_types.Part.from_uri(
                file_uri=uploaded.uri, mime_type=uploaded.mime_type
            ),
            prompt,
        ],
    )
    blocked = _is_content_blocked(None, response)
    if blocked is not None:
        raise ContentBlockedError(blocked["reason"], blocked["message"])
    return _parse_subtitles(response.text, offset_sec)


def _failure_type(reason):
    """Map a failure message to the pre-U2b error type taxonomy.

    Used to keep the error dict ``type`` field meaningful for callers that read
    it (e.g. the extraction result JSON): a ``429`` is a rate limit, a timeout /
    connection / 5xx message is transient, anything else is permanent.
    """
    text = str(reason).lower()
    if "429" in text:
        return "rate_limit"
    if any(
        marker in text
        for marker in ("408", "500", "502", "503", "504", "timeout", "connection")
    ):
        return "transient"
    return "permanent"


def call_with_rotation(keys, rotation, callable_, *args, call_budget=None, logger_=None):
    """Round-robin Gemini key rotation (thin wrapper over v2, U2b).

    Shared helper (used by B1 extraction and later chunks). The public
    contract is unchanged from before U2b so callers keep working:

    - On success returns ``(result, next_rotation, None)``.
    - On failure returns ``(None, rotation, error_dict)`` where ``error_dict``
      is ``{"type", "message", ...}``; callers treat a ``None`` result as a
      failure and apply their existing fallback/flagging. Never raises on a
      Gemini failure.

    Internally delegates to ``gemini_rotation.call_with_rotation_v2``:
    rotatable failures (rate limits, timeouts, unknown errors) rotate to the
    next key, non-rotatable failures (content-safety blocks, invalid requests)
    stop immediately, and the whole job run can share one
    ``gemini_rotation.CallBudget`` via ``call_budget`` (a ``None`` budget means
    unlimited, so existing callers are unaffected). The per-key failure log
    that v1 used to swallow is now recorded in the error dict's ``attempts``
    list and logged here.

    ``logger_`` (optional) is a per-job logger (see ``job_logging``) used
    instead of the module logger when the call happens inside a job pipeline,
    so Gemini failures land in ``uploads/<job_id>/logs/pipeline.log``.
    """
    log = logger_ or logger
    try:
        result, next_rotation = call_with_rotation_v2(
            keys, rotation, callable_, *args, call_budget=call_budget
        )
        return result, next_rotation, None
    except AllKeysExhausted as exc:
        attempts = list(exc.attempts)
        log.error("All %d active key(s) failed: %s", len(keys), attempts)
        reason = attempts[-1][1] if attempts else ""
        return None, rotation, {
            "type": _failure_type(reason),
            "message": reason,
            "attempts": attempts,
        }
    except CallBudgetExceeded as exc:
        log.error("Gemini call budget exceeded: %s", exc)
        return None, rotation, {
            "type": "call_budget_exceeded",
            "message": str(exc),
            "used": exc.used,
            "max_calls": exc.max_calls,
        }
    except Exception as exc:  # noqa: BLE001 - non-rotatable failures re-raised
        block = _is_content_blocked(exc)
        ftype = "content_blocked" if block is not None else "non_rotatable"
        log.error("Gemini call non-rotatable (%s): %s", ftype, exc)
        return None, rotation, {"type": ftype, "message": str(exc)}


def is_rate_limit_result(result):
    """True when a stage result/error dict is a rate-limit exhaustion (F15).

    Shared predicate across every Gemini call site (extraction, translation,
    voiceover, the segmented QA gate): :func:`call_with_rotation` catches
    ``AllKeysExhausted`` and returns an error dict whose ``type`` is
    ``"rate_limit"`` (see :func:`_failure_type`). A success result, ``None``,
    or any other error type (transient/network, malformed, content-blocked,
    unrelated) is not a rate-limit exhaustion. Deliberately extraction-
    agnostic: it only inspects the ``type`` field of the dict it is given.
    """
    return isinstance(result, dict) and result.get("type") == "rate_limit"


def _generate_with_rotation(
    keys, rotation, prompt, video_path, offset_sec, call_budget=None, logger_=None
):
    """Try keys round-robin; return (subtitles, next_rotation, error)."""
    return call_with_rotation(
        keys, rotation, _call_gemini, prompt, video_path, offset_sec,
        call_budget=call_budget, logger_=logger_,
    )


def _span(sub):
    return float(sub.get("end_sec", 0.0)) - float(sub.get("start_sec", 0.0))


def _dedup_merge(segment_results):
    """Merge per-segment subtitle lists, dropping overlap duplicates."""
    all_subs = []
    for subs in segment_results:
        all_subs.extend(subs)
    all_subs.sort(key=lambda s: float(s.get("start_sec", 0.0)))

    merged = []
    for sub in all_subs:
        start = float(sub.get("start_sec", 0.0))
        text = _normalize(sub.get("text"))
        if not text:
            merged.append(sub)
            continue
        duplicated = False
        idx = len(merged) - 1
        while idx >= 0 and float(merged[idx].get("start_sec", 0.0)) >= start - config.SUBTITLE_DEDUP_TOLERANCE_SEC:
            if _normalize(merged[idx].get("text")) == text:
                duplicated = True
                if _span(sub) > _span(merged[idx]):
                    merged[idx] = sub
                break
            idx -= 1
        if not duplicated:
            merged.append(sub)
    return merged


def _whisper_merge_subtitles(subtitles, whisper_segments):
    """Whisper-primary merge of Gemini text onto Whisper timing (F1-F3).

    Every Whisper segment becomes one subtitle entry anchored to the segment's
    timing. Its text is Gemini's (``text_source="gemini_cleaned"``) when an
    unused Gemini line overlaps the segment by at least
    ``SUBTITLE_OVERLAP_MATCH_MIN`` AND its normalized text is at least 0.3
    similar to the segment's (``SequenceMatcher`` ratio); otherwise the raw
    Whisper text is used (``text_source="whisper_raw"``). Gemini lines that
    overlap no Whisper segment at all are dropped and counted in the returned
    ``hallucinated_dropped`` count.

    Returns ``(merged, hallucinated_dropped)``.
    """
    used = [False] * len(subtitles)
    merged = []
    for segment in whisper_segments:
        seg_start = float(segment.get("start_sec", 0.0))
        seg_end = float(segment.get("end_sec", 0.0))
        seg_text = str(segment.get("text", "")).strip()
        best_index = None
        best_key = None
        for i, sub in enumerate(subtitles):
            if used[i]:
                continue
            ratio = overlap_ratio(
                sub.get("start_sec", 0.0), sub.get("end_sec", 0.0),
                seg_start, seg_end,
            )
            if ratio < config.SUBTITLE_OVERLAP_MATCH_MIN:
                continue
            sim = SequenceMatcher(
                None, _normalize(sub.get("text")), _normalize(seg_text)
            ).ratio()
            if sim < 0.3:
                continue
            if best_key is None or (ratio, sim) > best_key:
                best_key = (ratio, sim)
                best_index = i
        if best_index is not None:
            used[best_index] = True
            text = str(subtitles[best_index].get("text", "")).strip()
            text_source = "gemini_cleaned"
        else:
            text = seg_text
            text_source = "whisper_raw"
        if not text:
            continue
        merged.append(
            {
                "text": text,
                "start_sec": round(seg_start, 3),
                "end_sec": round(seg_end, 3),
                "text_source": text_source,
            }
        )

    hallucinated_dropped = 0
    for i, sub in enumerate(subtitles):
        if used[i]:
            continue
        if not _overlaps_any_segment(sub, whisper_segments):
            hallucinated_dropped += 1
    return merged, hallucinated_dropped


def _overlaps_any_segment(sub, whisper_segments):
    """True when a Gemini line's span overlaps any Whisper segment at all."""
    for segment in whisper_segments:
        if (
            overlap_ratio(
                sub.get("start_sec", 0.0), sub.get("end_sec", 0.0),
                segment.get("start_sec", 0.0), segment.get("end_sec", 0.0),
            )
            > 0
        ):
            return True
    return False


def _extract_audio(video_path):
    """Extract a mono 16kHz wav from a video for Whisper (F1-F3).

    The whole ``source.mp4`` becomes ``source_audio.wav``; a chunk segment
    ``segments/seg_XXX.mp4`` becomes ``segments/seg_XXX.wav`` (relative
    timestamps, shifted by the chunk offset at merge time).
    """
    if video_path.name == "source.mp4":
        wav_path = video_path.with_name("source_audio.wav")
    else:
        wav_path = video_path.with_suffix(".wav")
    _run_ffmpeg(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ar", "16000", "-ac", "1",
            "-c:a", "pcm_s16le", str(wav_path),
        ]
    )
    return wav_path


def _whisper_merge(job_dir, video_path, subtitles, offset_sec, job_logger,
                   allow_whisper=True):
    """Whisper-primary merge for one video (whole source or a chunk).

    ``video_path`` is the video Whisper transcribes; ``subtitles`` are the
    Gemini lines for it with ABSOLUTE timing; ``offset_sec`` is the video's
    start offset (0.0 for the whole source, the chunk start for a segment) so
    the Whisper segment times (relative to the video) are shifted to absolute
    before merging. ``allow_whisper=False`` (``gemini_only`` engine, F9)
    skips Whisper entirely — the pure-Gemini output is returned unchanged.

    Returns ``(merged_subtitles, stats)`` — on Whisper unavailability / no
    usable segments / failed audio extraction, returns ``(subtitles, None)``
    so the caller keeps today's pure-Gemini output unchanged.
    """
    if not subtitles or not allow_whisper:
        return subtitles, None
    try:
        wav_path = _extract_audio(video_path)
    except RuntimeError as exc:
        job_logger.warning(
            "job %s: whisper audio extraction failed for %s; "
            "skipping whisper merge: %s",
            job_dir.name, video_path.name, exc,
        )
        return subtitles, None

    segments = transcribe_segments(
        wav_path, model=config.WHISPER_MODEL_ZH, logger_=job_logger
    )
    if not segments:
        return subtitles, None

    for segment in segments:
        segment["start_sec"] = float(segment.get("start_sec", 0.0)) + offset_sec
        segment["end_sec"] = float(segment.get("end_sec", 0.0)) + offset_sec

    merged, hallucinated = _whisper_merge_subtitles(subtitles, segments)
    if not merged:
        return subtitles, None
    stats = {
        "whisper_used": True,
        "whisper_segments_count": len(segments),
        "gemini_hallucinated_dropped": hallucinated,
    }
    return merged, stats


def _build_result(job_id, status, chunked, segments_count, failed_segments, errors, subtitles):
    return {
        "job_id": job_id,
        "status": status,
        "chunked": chunked,
        "segments_count": segments_count,
        "failed_segments": list(failed_segments),
        "errors": {str(idx): info for idx, info in sorted(errors.items())},
        "subtitles": subtitles,
    }


def _save(job_dir, result):
    path = job_dir / "subtitles_zh_raw.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def extract_window(job_id, start_sec, end_sec, upload_root=None,
                   call_budget=None, logger_=None):
    """Re-extract subtitles for a specific time range ``[start_sec, end_sec)``.

    Cuts a clip from ``source.mp4`` via ffmpeg (``-ss``/``-to``/``-c copy``,
    exactly like ``_segment_video``), sends it to Gemini separately through
    ``call_with_rotation`` (key-rotation / content-block resilience), and
    returns a subtitle list with absolute timing (offset ``start_sec`` added
    back). The clip is written under ``job_dir/repair_segments/`` so it never
    collides with the main ``segments/`` or the group-A artifacts.

    Standalone (not wired into ``build_subtitle_list`` or app.py yet; wiring
    is B2/B3). Never raises on Gemini/parse failures: returns ``None`` when
    ffmpeg fails, no active keys, the rotated Gemini call fails, or the
    response is malformed. On success returns a list of subtitle dicts with
    ``text`` / ``start_sec`` / ``end_sec`` (absolute).
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    source = job_dir / "source.mp4"
    if not source.exists():
        return None

    keys = key_store.get_active_keys()
    if not keys:
        logger_.error("extract_window cannot start for job %s: no active Gemini keys", job_id) if logger_ else None
        return None

    repair_dir = job_dir / "repair_segments"
    repair_dir.mkdir(exist_ok=True)
    out = repair_dir / f"window_{start_sec:06.3f}_{end_sec:06.3f}.mp4"
    try:
        _run_ffmpeg(
            [
                "ffmpeg", "-y", "-ss", f"{start_sec:.3f}", "-to", f"{end_sec:.3f}",
                "-i", str(source), "-c", "copy", str(out),
            ]
        )
    except RuntimeError:
        return None

    subs, rotation, error = call_with_rotation(
        keys, 0, _call_gemini, SUBTITLE_EXTRACT_PROMPT, out, start_sec,
        call_budget=call_budget, logger_=logger_,
    )
    if subs is None:
        log = logger_ or logger
        log.error("extract_window for job %s window [%.3f, %.3f) failed: %s",
                  job_id, start_sec, end_sec, error)
        return None
    return subs


def extract_subtitles(job_id, upload_root=None, call_budget=None, progress_cb=None):
    """Extract Chinese subtitles for a job. Never raises on Gemini failures.

    ``progress_cb(processed, total)`` (optional) is called after every
    segment/chunk is handled, with the 1-based count of handled segments over
    the total, so the job-status wiring can report per-chunk progress (F9).

    F15 Part 2B: the ONE exception to "never raises" is quota exhaustion —
    when every configured Gemini key fails with a rate limit, the job is
    transitioned to ``api_limit_wait`` and :class:`job_status.ApiLimitWaitError`
    is raised so the stage stops instead of reporting ``extraction_failed``.
    Any other Gemini failure keeps the old never-raising behavior.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    source = job_dir / "source.mp4"
    if not source.exists():
        raise FileNotFoundError(f"no source.mp4 for job {job_id}")

    job_logger = job_logging.get_job_logger(job_id, upload_root)
    meta = _load_job_meta(job_dir)
    duration = meta.get("duration_sec")
    if duration is None:
        duration = video_ingest.probe_video(source).get("duration_sec")

    # F9: the per-job engine decides whether local Whisper may run at all.
    # gemini_only jobs skip Whisper even when it is installed.
    allow_whisper = engine_allows_whisper(job_id, upload_root)

    keys = key_store.get_active_keys()
    if not keys:
        job_logger.error("extraction cannot start for job %s: no active Gemini keys", job_id)
        result = _build_result(
            job_id, "extraction_failed", False, 0, [], {}, []
        )
        return _save(job_dir, result)

    threshold = config.LONG_VIDEO_CHUNK_THRESHOLD_SEC
    chunked = duration is not None and duration > threshold
    rotation = 0
    segment_results = []
    failed_segments = []
    errors = {}
    whisper_stats = []

    if not chunked:
        subs, rotation, error = _generate_with_rotation(
            keys, rotation, SUBTITLE_EXTRACT_PROMPT, source, 0.0,
            call_budget=call_budget, logger_=job_logger,
        )
        segments_count = 1
        if subs is None:
            if is_rate_limit_result(error):
                job_status.record_api_limit_wait(
                    job_id, "F1_extract", upload_root=upload_root,
                )
                raise job_status.ApiLimitWaitError(
                    f"All Gemini keys rate-limited during extraction for job {job_id}"
                )
            failed_segments.append(0)
            errors[0] = error
        else:
            subs, stats = _whisper_merge(
                job_dir, source, subs, 0.0, job_logger, allow_whisper
            )
            if stats:
                whisper_stats.append(stats)
            segment_results.append(subs)
        if progress_cb is not None:
            progress_cb(1, segments_count)
    else:
        segments = _segment_video(job_dir, source, duration)
        segments_count = len(segments)
        for seg in segments:
            subs, rotation, error = _generate_with_rotation(
                keys, rotation, SUBTITLE_EXTRACT_PROMPT, seg["path"], seg["start"],
                call_budget=call_budget, logger_=job_logger,
            )
            if subs is None:
                if is_rate_limit_result(error):
                    job_status.record_api_limit_wait(
                        job_id, "F1_extract", upload_root=upload_root,
                    )
                    raise job_status.ApiLimitWaitError(
                        f"All Gemini keys rate-limited during extraction for job {job_id}"
                    )
                failed_segments.append(seg["index"])
                errors[seg["index"]] = error
            else:
                subs, stats = _whisper_merge(
                    job_dir, seg["path"], subs, seg["start"], job_logger, allow_whisper
                )
                if stats:
                    whisper_stats.append(stats)
                segment_results.append(subs)
            if progress_cb is not None:
                progress_cb(len(segment_results) + len(failed_segments), segments_count)

    if len(failed_segments) == segments_count:
        status = "extraction_failed"
    elif failed_segments:
        status = "partial"
    else:
        status = "ok"

    subtitles = _dedup_merge(segment_results) if segment_results else []
    whisper_fields = {
        "whisper_used": bool(whisper_stats),
        "whisper_segments_count": sum(
            stats.get("whisper_segments_count", 0) for stats in whisper_stats
        ),
        "gemini_hallucinated_dropped": sum(
            stats.get("gemini_hallucinated_dropped", 0) for stats in whisper_stats
        ),
    }
    result = _build_result(
        job_id, status, chunked, segments_count, failed_segments, errors, subtitles
    )
    result.update(whisper_fields)
    return _save(job_dir, result)
