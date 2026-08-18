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
from datetime import datetime, timezone
from pathlib import Path

from pipeline import segmentation, video_ingest

ALLOWED_STATES = ("running", "done", "error")

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


def _atomic_write(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)
