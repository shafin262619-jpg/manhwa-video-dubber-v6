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
from pathlib import Path

from pipeline import video_ingest

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
