"""Job status tracking (U1a infrastructure).

Provides a small, thread-safe status store for jobs so clients can poll
progress once the pipeline is wired to write it (U1b/U1c). Each job's status
lives in ``uploads/<job_id>/job_status.json`` and holds a flat
``stage``/``state`` view plus a per-stage ``stages`` history map.

Nothing in the pipeline writes status here yet — this chunk only installs the
infrastructure and a read-only polling endpoint. Mirrors the get_lock() /
update-status pattern from BlueprintTube's app.py, factored into its own
module so both app.py and the pipeline package can import it without an
import cycle.
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import segmentation, video_ingest

# ``api_limit_wait`` (F15 Part 2A): a stage cannot make progress because every
# configured Gemini key failed with a rate limit until the daily quota resets.
# Additive — existing states are never renamed or removed.
ALLOWED_STATES = ("running", "done", "error", "api_limit_wait")

DEFAULT_STATUS = {"stage": "unknown", "state": "not_started"}

# One lock per job_id guards the read-modify-write of that job's status file.
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def status_path(job_id, upload_root=None):
    """Return the ``job_status.json`` path for a job under the upload root."""
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    return root / job_id / "job_status.json"


def _lock_for(job_id):
    with _LOCKS_GUARD:
        lock = _LOCKS.get(job_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[job_id] = lock
        return lock


def read_status(job_id, upload_root=None):
    """Read the persisted job status; the default dict when no file exists.

    Never raises: a missing or unreadable file yields
    ``{"stage": "unknown", "state": "not_started"}``.
    """
    path = status_path(job_id, upload_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_STATUS)
    if not isinstance(data, dict):
        return dict(DEFAULT_STATUS)
    return data


def write_status(job_id, stage, state, extra=None, upload_root=None):
    """Record a stage transition, preserving per-stage history.

    The status file holds ``{"stage": ..., "state": ..., "stages": {...}}``.
    Each call merges the existing file so older stages are never dropped, then
    writes atomically (temp file + ``os.replace``). ``state`` must be one of
    ``running`` / ``done`` / ``error``; ``extra`` may carry progress details
    such as ``processed_count`` / ``total_count``.
    """
    if state not in ALLOWED_STATES:
        raise ValueError(
            f"invalid state {state!r}; expected one of {ALLOWED_STATES}"
        )
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        stages = data.get("stages")
        if not isinstance(stages, dict):
            stages = {}
        entry = {"stage": stage, "state": state}
        if extra:
            entry.update(extra)
        stages[stage] = entry
        data["stages"] = stages
        data["stage"] = stage
        data["state"] = state

        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)


def run_stage(job_id, stage, func, *args, **kwargs):
    """Run a pipeline stage with ``running`` / ``done`` / ``error`` status.

    Writes ``stage`` running before calling ``func(*args, **kwargs)`` and
    ``done`` after it returns; on exception writes ``error`` and re-raises so
    the caller keeps its existing error handling. A ``progress`` dict written
    into the stage entry by the func (via :func:`write_status`) is preserved
    on the ``done`` entry so progress survives the state transition.

    Status writes are best-effort: a failure to write status (e.g. disk
    error) never hides the stage's result or its error.
    """
    try:
        write_status(job_id, stage, "running")
    except Exception:  # noqa: BLE001 - status is advisory, never blocking
        pass
    try:
        result = func(*args, **kwargs)
    except Exception:
        try:
            write_status(job_id, stage, "error")
        except Exception:  # noqa: BLE001
            pass
        raise
    try:
        progress = read_status(job_id).get("stages", {}).get(stage, {}).get("progress")
    except Exception:  # noqa: BLE001
        progress = None
    extra = {"progress": progress} if progress else None
    try:
        write_status(job_id, stage, "done", extra=extra)
    except Exception:  # noqa: BLE001
        pass
    return result


# ---------------------------------------------------------------------------
# Per-segment tracking (F13b)
#
# A segmented job extends the status file with a ``segments`` map (one entry
# per segment, each with its own ``stage`` / ``state`` / ``stages`` history,
# plus the segment's time range, completion timestamp and output path) and a
# ``segmented`` block summarising the overall progress. The top-level
# ``stage``/``state`` are kept in sync with the overall segmented state so the
# existing polling page keeps working.
# ---------------------------------------------------------------------------

SEGMENT_STAGE_INIT = "pending"


def init_segments(job_id, plan, upload_root=None):
    """Record a segment plan in the status file (once per job).

    Adds ``data["segmented"]`` (enabled / strategy / durations / counts /
    overall state) and ``data["segments"]`` (one entry per segment). Callers
    should call this before processing starts so per-segment progress has a
    home to be written into.
    """
    segments = plan.get("segments") or []
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        seg_map = {}
        for seg in segments:
            key = segmentation.segment_key(seg["index"])
            seg_map[key] = {
                "index": int(seg["index"]),
                "start_sec": seg.get("start_sec"),
                "end_sec": seg.get("end_sec"),
                "stage": SEGMENT_STAGE_INIT,
                "state": SEGMENT_STAGE_INIT,
                "stages": {},
            }
        data["segments"] = seg_map
        data["segmented"] = {
            "enabled": True,
            "strategy": plan.get("strategy"),
            "target_duration_sec": plan.get("target_duration_sec"),
            "source_duration_sec": plan.get("source_duration_sec"),
            "total_count": len(seg_map),
            "completed_count": 0,
            "overall_state": "running",
            "review_state": SEGMENT_REVIEW_IN_REVIEW,
        }
        data["stage"] = "segmented_pipeline"
        data["state"] = "running"
        _atomic_write(path := status_path(job_id, upload_root), data)


def write_segment_status(job_id, seg_index, stage, state, extra=None,
                         upload_root=None):
    """Record a stage transition for one segment, preserving its history.

    Mirrors :func:`write_status` but scoped to ``data["segments"]["seg_XXX"]``:
    the entry's ``stages`` map keeps every transition for that segment and the
    entry's ``stage``/``state`` mirror the latest one. The top-level
    ``segmented`` summary (completed count / overall state) is recomputed.
    """
    if state not in ALLOWED_STATES:
        raise ValueError(
            f"invalid state {state!r}; expected one of {ALLOWED_STATES}"
        )
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        seg_map = data.get("segments")
        if not isinstance(seg_map, dict):
            seg_map = {}
            data["segments"] = seg_map
        key = segmentation.segment_key(seg_index)
        entry = seg_map.get(key)
        if not isinstance(entry, dict):
            entry = {"index": int(seg_index), "stages": {}}
            seg_map[key] = entry
        stages = entry.get("stages")
        if not isinstance(stages, dict):
            stages = {}
            entry["stages"] = stages
        stage_entry = {"stage": stage, "state": state}
        if extra:
            stage_entry.update(extra)
        stages[stage] = stage_entry
        entry["stage"] = stage
        entry["state"] = state
        _update_segmented_overall(data)
        _atomic_write(path, data)


def mark_segment_done(job_id, seg_index, final_path=None, upload_root=None,
                      extra=None):
    """Mark one segment fully complete (terminal ``done`` + output path).

    Sets the segment's ``state`` to ``done``, records ``completed_at``
    (UTC ISO) and ``final_path`` (the segment's final video, when known), and
    recomputes the overall segmented summary.
    """
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        seg_map = data.get("segments")
        if not isinstance(seg_map, dict):
            seg_map = {}
            data["segments"] = seg_map
        key = segmentation.segment_key(seg_index)
        entry = seg_map.get(key)
        if not isinstance(entry, dict):
            entry = {"index": int(seg_index), "stages": {}}
            seg_map[key] = entry
        entry["stage"] = entry.get("stage") or "complete"
        entry["state"] = "done"
        entry["completed_at"] = datetime.now(timezone.utc).isoformat()
        if final_path is not None:
            entry["final_path"] = str(final_path)
        if extra:
            entry.update(extra)
        _update_segmented_overall(data)
        _atomic_write(path, data)


def mark_segment_error(job_id, seg_index, message=None, upload_root=None):
    """Mark one segment failed (terminal ``error`` + message)."""
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        seg_map = data.get("segments")
        if not isinstance(seg_map, dict):
            seg_map = {}
            data["segments"] = seg_map
        key = segmentation.segment_key(seg_index)
        entry = seg_map.get(key)
        if not isinstance(entry, dict):
            entry = {"index": int(seg_index), "stages": {}}
            seg_map[key] = entry
        entry["stage"] = entry.get("stage") or "error"
        entry["state"] = "error"
        if message is not None:
            entry["error_detail"] = str(message)[:500]
        _update_segmented_overall(data)
        _atomic_write(path, data)


def read_segment_status(job_id, seg_index, upload_root=None):
    """Read one segment's status entry (or a default when absent/unknown)."""
    key = segmentation.segment_key(seg_index)
    data = read_status(job_id, upload_root)
    entry = data.get("segments", {}).get(key)
    if not isinstance(entry, dict):
        return {"index": int(seg_index), "stage": SEGMENT_STAGE_INIT,
                "state": SEGMENT_STAGE_INIT, "stages": {}}
    return entry


def segmented_summary(job_id, upload_root=None):
    """The ``segmented`` block (or ``None`` when the job is not segmented)."""
    data = read_status(job_id, upload_root)
    return data.get("segmented")


def _update_segmented_overall(data):
    """Recompute ``segmented.completed_count``/``overall_state`` + top state."""
    seg_map = data.get("segments")
    if not isinstance(seg_map, dict) or not seg_map:
        return
    total = len(seg_map)
    states = [
        entry.get("state") for entry in seg_map.values() if isinstance(entry, dict)
    ]
    completed = sum(1 for s in states if s == "done")
    if "error" in states:
        overall = "error"
    elif completed == total:
        overall = "done"
    else:
        overall = "running"
    segmented = data.get("segmented")
    if not isinstance(segmented, dict):
        segmented = {}
        data["segmented"] = segmented
    segmented["completed_count"] = completed
    segmented["total_count"] = total
    segmented["overall_state"] = overall
    data["state"] = overall


# ---------------------------------------------------------------------------
# Per-segment review recording (F14a/Part1)
#
# As each segment finishes, the user can report problems with it, possibly
# across multiple fix rounds (a future chunk adds rounds 2+; the schema
# supports them from the start). Reviews live under
# ``data["segments"]["seg_XXX"]["reviews"]``, a map keyed by round number so
# rounds never overwrite each other. A round entry with an empty ``issues``
# list is the explicit "reviewed, no issues" state — distinct from a segment
# that has never been reviewed (no entry at all). Recording a review only
# touches the given segment's entry, so it never disturbs the processing
# state of any other segment.
# ---------------------------------------------------------------------------

SEGMENT_REVIEW_ISSUE_CATEGORIES = {
    "timing_mismatch": "ভয়েস ও দৃশ্যের টাইমিং মিসম্যাচ",
    "bad_translation": "ভুল বা দুর্বল অনুবাদ",
    "subtitle_timing": "সাবটাইটেল টাইমিং ঠিক নেই",
    "tts_quality": "উচ্চারণ বা ভয়েস কোয়ালিটি সমস্যা",
    "audio_glitch": "অডিও কোয়ালিটি সমস্যা",
    "other": "অন্য কোনো সমস্যা (ফ্রি টেক্সট)",
}

DEFAULT_REVIEW_ROUND = 1


def record_segment_review(job_id, seg_index, issues=None,
                          round_no=DEFAULT_REVIEW_ROUND, notes=None,
                          upload_root=None):
    """Record a per-segment issue report for one review round.

    Writes ``data["segments"]["seg_XXX"]["reviews"][round]`` holding the list
    of issue tags, optional free text and a UTC timestamp. ``issues`` must be
    a list of :data:`SEGMENT_REVIEW_ISSUE_CATEGORIES` keys; an empty list (or
    ``None``) records the explicit "reviewed, no issues" state for this round.
    Multiple rounds coexist under ``reviews`` and never overwrite each other.
    Only the given segment's entry is modified — the processing state of other
    segments is left untouched.
    """
    unknown = [
        tag for tag in (issues or []) if tag not in SEGMENT_REVIEW_ISSUE_CATEGORIES
    ]
    if unknown:
        raise ValueError(
            f"invalid issue tag(s) {unknown!r}; expected one of "
            f"{sorted(SEGMENT_REVIEW_ISSUE_CATEGORIES)}"
        )
    if round_no < 1:
        raise ValueError(f"invalid review round {round_no!r}; rounds start at 1")
    seen = set()
    clean = []
    for tag in issues or []:
        if tag not in seen:
            seen.add(tag)
            clean.append(tag)
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        seg_map = data.get("segments")
        if not isinstance(seg_map, dict):
            seg_map = {}
            data["segments"] = seg_map
        key = segmentation.segment_key(seg_index)
        entry = seg_map.get(key)
        if not isinstance(entry, dict):
            entry = {"index": int(seg_index), "stages": {}}
            seg_map[key] = entry
        reviews = entry.get("reviews")
        if not isinstance(reviews, dict):
            reviews = {}
            entry["reviews"] = reviews
        round_entry = {
            "round": int(round_no),
            "issues": clean,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        if notes:
            round_entry["notes"] = str(notes)
        reviews[str(int(round_no))] = round_entry
        _atomic_write(path, data)


def get_segment_reviews(job_id, seg_index, round_no=None, upload_root=None):
    """Return recorded reviews for one segment.

    With ``round_no`` returns that round's entry (or ``None`` when the segment
    has no review for that round yet). Without it returns the full per-round
    map (``{}`` when nothing has been recorded). An entry whose ``issues`` is
    ``[]`` is the explicit "reviewed, no issues" state; a missing round means
    the segment has not been reviewed in that round.
    """
    key = segmentation.segment_key(seg_index)
    data = read_status(job_id, upload_root)
    entry = data.get("segments", {}).get(key)
    if isinstance(entry, dict) and isinstance(entry.get("reviews"), dict):
        reviews = entry["reviews"]
    else:
        reviews = {}
    if round_no is None:
        return reviews
    return reviews.get(str(int(round_no)))


def next_review_round(job_id, seg_index, upload_root=None):
    """The next free review-round number for a segment (max round + 1)."""
    reviews = get_segment_reviews(job_id, seg_index, upload_root=upload_root)
    rounds = [int(key) for key in reviews if str(key).isdigit()]
    return (max(rounds) if rounds else 0) + 1


# ``rerun_status`` values for a correction round (F14b).
SEGMENT_RERUN_OK = "ok"
SEGMENT_RERUN_FAILED = "failed"


def record_segment_rerun(job_id, seg_index, *, triggered_by_round, issues,
                         target_stage, status=SEGMENT_RERUN_OK,
                         error_message=None, correction=None,
                         upload_root=None):
    """Record a targeted correction re-run as the segment's next round (F14b).

    Writes ``data["segments"]["seg_XXX"]["reviews"][round]`` where ``round``
    is ``next_review_round`` — one correction attempt produces exactly one new
    round, and later rounds never overwrite earlier ones (no automatic retry
    loop). The round entry reuses the F14a review schema (``round`` /
    ``issues`` / timestamp) and adds ``rerun`` markers: ``triggered_by_round``
    (the reviewed round this re-run corrects), ``target_stage`` (the owning
    pipeline stage), ``rerun_status`` (``ok`` / ``failed``), an optional
    Bengali ``rerun_error_bn`` for failed attempts and the ``correction``
    instruction built for the stage. Only the given segment's entry is
    modified; other segments are untouched. Returns the round number written.
    """
    if status not in (SEGMENT_RERUN_OK, SEGMENT_RERUN_FAILED):
        raise ValueError(f"invalid rerun status {status!r}")
    unknown = [
        tag for tag in (issues or []) if tag not in SEGMENT_REVIEW_ISSUE_CATEGORIES
    ]
    if unknown:
        raise ValueError(
            f"invalid issue tag(s) {unknown!r}; expected one of "
            f"{sorted(SEGMENT_REVIEW_ISSUE_CATEGORIES)}"
        )
    seen = set()
    clean = []
    for tag in issues or []:
        if tag not in seen:
            seen.add(tag)
            clean.append(tag)
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        seg_map = data.get("segments")
        if not isinstance(seg_map, dict):
            seg_map = {}
            data["segments"] = seg_map
        key = segmentation.segment_key(seg_index)
        entry = seg_map.get(key)
        if not isinstance(entry, dict):
            entry = {"index": int(seg_index), "stages": {}}
            seg_map[key] = entry
        reviews = entry.get("reviews")
        if not isinstance(reviews, dict):
            reviews = {}
            entry["reviews"] = reviews
        round_no = next_review_round(job_id, seg_index, upload_root)
        round_entry = {
            "round": round_no,
            "issues": clean,
            "rerun": True,
            "rerun_of_round": int(triggered_by_round),
            "target_stage": str(target_stage),
            "rerun_status": status,
            "rerun_at": datetime.now(timezone.utc).isoformat(),
        }
        if error_message is not None:
            round_entry["rerun_error_bn"] = str(error_message)[:500]
        if correction is not None:
            round_entry["correction"] = str(correction)
        reviews[str(round_no)] = round_entry
        _atomic_write(path, data)
    return round_no


def restore_segment_state(job_id, seg_index, prior_entry, upload_root=None):
    """Restore a segment's processing state to a snapshot (F14b rollback).

    Rewrites ``stage`` / ``state`` / ``stages`` / ``final_path`` /
    ``completed_at`` / ``error_detail`` from ``prior_entry`` (the entry read
    before a correction attempt) so a failed re-run leaves the segment at its
    last-good round's state. ``reviews`` is always preserved — a failed
    attempt's rerun round must survive the rollback. The top-level
    ``segmented`` summary is recomputed.
    """
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        key = segmentation.segment_key(seg_index)
        entry = data.get("segments", {}).get(key)
        if not isinstance(entry, dict):
            return
        reviews = entry.get("reviews")
        restored = dict(prior_entry) if isinstance(prior_entry, dict) else {}
        for field in (
            "stage", "state", "stages", "final_path", "completed_at",
            "error_detail",
        ):
            if field in restored:
                entry[field] = restored[field]
            else:
                entry.pop(field, None)
        if isinstance(reviews, dict):
            entry["reviews"] = reviews
        _update_segmented_overall(data)
        _atomic_write(path, data)


# ---------------------------------------------------------------------------
# Per-segment automated pre-review QA gate (F14b Part 2)
#
# Before a segment is released to human review, an automated Gemini check
# verifies voice/scene sync for every dialogue line and auto-fixes mismatches.
# Its state lives on the segment entry as ``data["segments"]["seg_XXX"]["qa"]``
# — an extension of the existing per-segment schema, not a parallel one:
#
#   "qa": {
#     "state": "qa_checking" | "qa_fixing_attempt_N" | "qa_passed" | "qa_capped",
#     "attempts": [
#       {"attempt": 1, "outcome": "mismatch", "issues": [3, 7],
#        "fixed": true, "at": "<iso>"},
#       {"attempt": 2, "outcome": "pass", "fixed": false, "at": "<iso>"}
#     ],
#     "note_bn": "<Bengali note when capped>"
#   }
#
# The attempts log mirrors F12b's per-window gap-fill log: every Gemini check
# round is recorded with its outcome and whether a fix was applied, so the
# gate's progress is inspectable rather than silent.
# ---------------------------------------------------------------------------

SEGMENT_QA_CHECKING = "qa_checking"
SEGMENT_QA_PASSED = "qa_passed"
SEGMENT_QA_CAPPED = "qa_capped"

# Bengali note attached when the automated QA cap is reached (F14b Part 2);
# surfaced in F14a's review UI so the user knows to look closely.
SEGMENT_QA_CAP_NOTE_BN = (
    "স্বয়ংক্রিয় প্রাক-রিভিউ চেক একটি সম্ভাব্য সমস্যা খুঁজে পেয়েছে "
    "যা স্বয়ংক্রিয়ভাবে পুরোপুরি সমাধান করা যায়নি — এই সেগমেন্টটি "
    "একটু মনোযোগ দিয়ে দেখে নিন।"
)


def record_segment_qa(job_id, seg_index, state, *, attempt=None, outcome=None,
                      issues=None, fixed=None, error_bn=None, note_bn=None,
                      upload_root=None):
    """Record the automated pre-review QA gate state for one segment (F14b).

    Writes ``data["segments"]["seg_XXX"]["qa"]["state"]``. When ``attempt`` is
    given, an entry is appended to the ``qa.attempts`` log — one entry per
    Gemini check round, recording its ``outcome`` (``pass`` / ``mismatch`` /
    ``failed``), the failing serials and whether a targeted re-run fix was
    applied. ``note_bn`` sets the Bengali note shown in F14a when the cap is
    reached. Only the given segment's entry is modified; other segments are
    untouched.
    """
    if attempt is not None and attempt < 1:
        raise ValueError(f"invalid QA attempt {attempt!r}; attempts start at 1")
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        seg_map = data.get("segments")
        if not isinstance(seg_map, dict):
            seg_map = {}
            data["segments"] = seg_map
        key = segmentation.segment_key(seg_index)
        entry = seg_map.get(key)
        if not isinstance(entry, dict):
            entry = {"index": int(seg_index), "stages": {}}
            seg_map[key] = entry
        qa = entry.get("qa")
        if not isinstance(qa, dict):
            qa = {}
            entry["qa"] = qa
        qa["state"] = state
        if note_bn:
            qa["note_bn"] = str(note_bn)
        if attempt is not None:
            attempts = qa.get("attempts")
            if not isinstance(attempts, list):
                attempts = []
                qa["attempts"] = attempts
            attempt_entry = {
                "attempt": int(attempt),
                "outcome": outcome,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            if issues:
                attempt_entry["issues"] = [int(i) for i in issues]
            if fixed is not None:
                attempt_entry["fixed"] = bool(fixed)
            if error_bn:
                attempt_entry["error_bn"] = str(error_bn)
            attempts.append(attempt_entry)
        _atomic_write(path, data)


def get_segment_qa(job_id, seg_index, upload_root=None):
    """The segment's ``qa`` block (or ``{}`` when never QA-checked)."""
    entry = read_segment_status(job_id, seg_index, upload_root)
    qa = entry.get("qa")
    return qa if isinstance(qa, dict) else {}


# ---------------------------------------------------------------------------
# All-segments-done-reviewing + final video assembly state (F14c Part 1)
#
# Once every segment has reached "done-reviewing" (its latest review round is
# a clean human review — no issues, not a rerun), the job-wide final video is
# assembled by concatenating each segment's latest-round output in order.
# The overall review state lives on the ``segmented`` block as
# ``review_state`` (in_review -> final_ready -> assembly_failed), and the
# assembly result (path / version / per-segment rounds / error) lives in
# ``segmented.final_assembly``. Both are additive: the existing
# ``overall_state`` (processing progress) is never touched here.
# ---------------------------------------------------------------------------

# ``segmented.review_state`` values (F14c Part 1 + Part 2).
SEGMENT_REVIEW_IN_REVIEW = "in_review"
SEGMENT_REVIEW_FINAL_READY = "final_ready"
SEGMENT_REVIEW_ASSEMBLY_FAILED = "assembly_failed"
SEGMENT_REVIEW_CONFIRMED = "confirmed"

# ``segmented.final_assembly.state`` values.
SEGMENT_ASSEMBLY_READY = "ready"
SEGMENT_ASSEMBLY_STALE = "stale"
SEGMENT_ASSEMBLY_FAILED = "failed"


def segment_latest_review_round(entry):
    """The latest review-round number recorded for a segment entry, or None."""
    if not isinstance(entry, dict):
        return None
    reviews = entry.get("reviews")
    if not isinstance(reviews, dict):
        return None
    rounds = [
        int(key) for key in reviews
        if str(key).isdigit() and isinstance(reviews.get(key), dict)
    ]
    return max(rounds) if rounds else None


def segment_latest_review_rounds(job_id, upload_root=None):
    """Map ``seg_XXX`` -> latest review round for every segment (no round =
    ``None``)."""
    data = read_status(job_id, upload_root)
    seg_map = data.get("segments")
    if not isinstance(seg_map, dict):
        return {}
    return {
        key: segment_latest_review_round(entry)
        for key, entry in seg_map.items()
    }


def _entry_review_complete(entry):
    """True when a segment's LATEST review round is a clean human review.

    A rerun round (``rerun: True``) is never done-reviewing, and neither is a
    round that reported issues — those both mean the segment is mid-fix or
    awaiting a fresh human verdict on the corrected output.
    """
    latest_round = segment_latest_review_round(entry)
    if latest_round is None:
        return False
    reviews = entry.get("reviews") if isinstance(entry, dict) else {}
    latest = reviews.get(str(latest_round))
    if not isinstance(latest, dict):
        return False
    if latest.get("rerun"):
        return False
    return not latest.get("issues")


def is_segment_review_complete(job_id, seg_index, upload_root=None):
    """True when the given segment has reached done-reviewing (F14c)."""
    return _entry_review_complete(read_segment_status(job_id, seg_index, upload_root))


def all_segments_review_complete(job_id, upload_root=None):
    """True only when EVERY segment's latest round is done-reviewing.

    Never raises and never errors on partially-processed jobs: a missing
    segment map, a segment still processing/awaiting review, or a segment
    mid-fix simply returns False. A single-segment job is handled the same as
    any other — it is complete only once that one segment is reviewed clean.
    """
    data = read_status(job_id, upload_root)
    seg_map = data.get("segments")
    if not isinstance(seg_map, dict) or not seg_map:
        return False
    for entry in seg_map.values():
        if not _entry_review_complete(entry):
            return False
    return True


def invalidate_final_assembly(job_id, upload_root=None):
    """Revert the overall review state when a NEW issue is reported (F14c).

    Called right after an issue report lands on a segment that may already
    have a job-wide final video: the assembled video is no longer current, so
    ``review_state`` returns to ``in_review`` and the recorded assembly block
    is marked ``stale`` (the file itself is left for the user to inspect).
    """
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        segmented = data.get("segmented")
        if not isinstance(segmented, dict):
            segmented = {}
            data["segmented"] = segmented
        segmented["review_state"] = SEGMENT_REVIEW_IN_REVIEW
        assembly = segmented.get("final_assembly")
        if isinstance(assembly, dict):
            assembly["state"] = SEGMENT_ASSEMBLY_STALE
            assembly["invalidated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(path, data)


def mark_final_assembly_ready(job_id, final_path, segment_rounds, version,
                              upload_root=None):
    """Record a successful job-wide assembly as the current final video."""
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        segmented = data.get("segmented")
        if not isinstance(segmented, dict):
            segmented = {}
            data["segmented"] = segmented
        segmented["review_state"] = SEGMENT_REVIEW_FINAL_READY
        segmented["final_assembly"] = {
            "state": SEGMENT_ASSEMBLY_READY,
            "final_path": str(final_path),
            "version": int(version),
            "assembled_at": datetime.now(timezone.utc).isoformat(),
            "segment_rounds": dict(segment_rounds or {}),
        }
        _atomic_write(path, data)


def mark_final_assembly_failed(job_id, error_bn, upload_root=None):
    """Record a failed assembly: Bengali error + retryable state."""
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        segmented = data.get("segmented")
        if not isinstance(segmented, dict):
            segmented = {}
            data["segmented"] = segmented
        segmented["review_state"] = SEGMENT_REVIEW_ASSEMBLY_FAILED
        assembly = segmented.get("final_assembly")
        if not isinstance(assembly, dict):
            assembly = {}
            segmented["final_assembly"] = assembly
        assembly["state"] = SEGMENT_ASSEMBLY_FAILED
        assembly["error_bn"] = str(error_bn)[:500]
        assembly["error_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(path, data)


def mark_final_confirmed(job_id, upload_root=None):
    """Record the terminal "user confirmed the final video" state (F14c Part 2).

    Sets ``segmented.review_state`` to ``confirmed`` and writes the
    ``segmented.final_confirmation`` block (``user_confirmed`` + UTC
    ``confirmed_at``). This is the definitive end of the job's processing:
    the page stops offering any further review/fix/confirm controls. The
    recorded ``final_assembly`` (path/version) is left untouched — the
    confirmed video stays viewable/downloadable.
    """
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        segmented = data.get("segmented")
        if not isinstance(segmented, dict):
            segmented = {}
            data["segmented"] = segmented
        segmented["review_state"] = SEGMENT_REVIEW_CONFIRMED
        segmented["final_confirmation"] = {
            "user_confirmed": True,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write(path, data)


def _atomic_write(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# API rate-limit exhaustion wait (F15 Part 2A)
#
# When every configured Gemini key fails with a rate limit (429), the affected
# stage cannot make progress until the daily quota resets. ``api_limit_wait``
# is an allowed state (see :data:`ALLOWED_STATES`) a stage can transition to,
# and the details live in a top-level ``api_limit_wait`` block — additive,
# exactly like F14c's ``segmented.final_assembly``:
#
#   "api_limit_wait": {
#     "stage": "<stage that hit the limit>",
#     "hit_at": "<iso>",
#     "next_retry_at": "<iso>",
#     "attempt_count": 2
#   }
#
# ``next_retry_at`` follows :func:`compute_next_retry`: the first hit waits a
# full 24h quota window, each later hit re-queues one hour after the previous
# attempt's retry time. No rendering/UI or pipeline wiring exists yet — the
# four Gemini call sites that can produce a ``rate_limit`` result dict consume
# this in a later chunk.
# ---------------------------------------------------------------------------


def _as_utc_datetime(value):
    """Coerce a datetime or ISO-8601 string to a timezone-aware UTC datetime.

    A naive datetime is treated as UTC; ``datetime.fromisoformat`` (3.11)
    also accepts a trailing ``Z``.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError(
            f"expected datetime or ISO-8601 string, got {type(value).__name__}"
        )
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def compute_next_retry(hit_at, attempt_number):
    """The next retry time after a rate-limit exhaustion hit.

    Attempt 1 waits a full daily-quota window (``hit_at + 24h``); every later
    attempt re-queues one hour after the previous attempt's retry time, so
    attempt 2 is ``hit_at + 25h``, attempt 3 ``hit_at + 26h``, and so on —
    never another full 24h. ``hit_at`` may be a timezone-aware datetime or an
    ISO-8601 string. Returns a timezone-aware UTC datetime.
    """
    attempt = int(attempt_number)
    if attempt < 1:
        raise ValueError(
            f"invalid attempt number {attempt_number!r}; attempts start at 1"
        )
    hit = _as_utc_datetime(hit_at)
    return hit + timedelta(hours=24 + (attempt - 1))


def record_api_limit_wait(job_id, stage, *, hit_at=None, attempt_count=None,
                          upload_root=None):
    """Record that a stage hit API rate-limit exhaustion (F15 Part 2A).

    Writes the top-level ``api_limit_wait`` block (``stage`` / ``hit_at`` /
    ``next_retry_at`` / ``attempt_count``) and transitions that stage's entry
    plus the top-level ``stage``/``state`` to ``api_limit_wait`` so the polling
    endpoint reflects the wait. When ``attempt_count`` is omitted it
    increments the previously recorded count (first hit -> 1, next -> 2, ...);
    ``hit_at`` defaults to now. ``next_retry_at`` is computed with
    :func:`compute_next_retry`. Returns the recorded block.
    """
    hit = _as_utc_datetime(hit_at) if hit_at is not None else datetime.now(timezone.utc)
    prior = get_api_limit_wait(job_id, upload_root)
    if attempt_count is not None:
        count = int(attempt_count)
    else:
        count = int(prior.get("attempt_count", 0)) + 1 if prior else 1
    if count < 1:
        raise ValueError(
            f"invalid attempt count {attempt_count!r}; attempts start at 1"
        )
    next_retry = compute_next_retry(hit, count)
    block = {
        "stage": str(stage),
        "hit_at": hit.isoformat(),
        "next_retry_at": next_retry.isoformat(),
        "attempt_count": count,
    }
    path = status_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(job_id):
        data = read_status(job_id, upload_root)
        data["api_limit_wait"] = block
        stages = data.get("stages")
        if not isinstance(stages, dict):
            stages = {}
            data["stages"] = stages
        entry = stages.get(str(stage))
        if not isinstance(entry, dict):
            entry = {"stage": str(stage), "stages": {}}
            stages[str(stage)] = entry
        entry["state"] = "api_limit_wait"
        entry["hit_at"] = block["hit_at"]
        entry["next_retry_at"] = block["next_retry_at"]
        entry["attempt_count"] = count
        data["stage"] = str(stage)
        data["state"] = "api_limit_wait"
        _atomic_write(path, data)
    return block


def get_api_limit_wait(job_id, upload_root=None):
    """The recorded ``api_limit_wait`` block (or ``None`` when never recorded)."""
    data = read_status(job_id, upload_root)
    block = data.get("api_limit_wait")
    return block if isinstance(block, dict) else None
