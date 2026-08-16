"""Job history store (F9).

Tracks the recent jobs so the app can list them (history) and so a job that
would push the history past its cap cannot silently evict an older one. The
index lives at ``uploads/_history_index.json`` and is capped at
``HISTORY_LIMIT`` (3) entries:

- ``register_job`` puts a new job at the front (newest first). When the index
  is already full it does NOT evict: it returns ``{"added": False,
  "would_evict": <oldest_job_id>, "needs_confirm": True}`` so the caller can
  ask the user first (two-step confirm flow in the app, F9 spec: "4th job →
  HTTP 409 + confirm dialog, never silent eviction").
- ``evict_job`` drops a job from the index, optionally deleting its job dir
  from disk (``delete_files=True``) — the app only does this after the user
  confirms.
- ``list_history`` returns the indexed jobs newest-first, reading live
  per-job metadata (job_config, status, voice source). Jobs whose dirs are
  gone (evicted with delete) are skipped.

All reads are never-raising; all writes are atomic (temp file + os.replace).
"""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pipeline import job_config, job_status, video_ingest, voiceover_unify

HISTORY_LIMIT = 3


def _index_path(upload_root=None):
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    return root / "_history_index.json"


def _load_index(upload_root=None):
    path = _index_path(upload_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("job_id")]


def _save_index(entries, upload_root=None):
    path = _index_path(upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def _job_dir(job_id, upload_root=None):
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    return root / job_id


def _load_job_meta(job_id, upload_root=None):
    path = _job_dir(job_id, upload_root) / "job_meta.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry_for(job_id, meta=None, upload_root=None):
    meta = meta or {}
    name = meta.get("target_video_name") or _load_job_meta(
        job_id, upload_root
    ).get("source_filename")
    return {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_video_name": name,
    }


def register_job(job_id, meta=None, upload_root=None):
    """Register a new job in the history index (newest first).

    Returns ``{"added": True}`` when the job fits under ``HISTORY_LIMIT``, or
    ``{"added": False, "would_evict": <oldest_job_id>, "needs_confirm": True}``
    when the index is already full — the caller must ask the user before
    evicting anything (never a silent eviction).
    """
    entries = _load_index(upload_root)
    entries = [e for e in entries if e.get("job_id") != job_id]
    if len(entries) >= HISTORY_LIMIT:
        return {
            "added": False,
            "would_evict": entries[-1]["job_id"],
            "needs_confirm": True,
        }
    entries.insert(0, _entry_for(job_id, meta, upload_root))
    _save_index(entries, upload_root)
    return {"added": True}


def evict_job(job_id, delete_files=True, upload_root=None):
    """Drop a job from the history index, optionally deleting its files.

    Only called after the user confirms (two-step confirm flow). Returns
    ``{"evicted": bool}`` — False when the job was not in the index.
    """
    entries = _load_index(upload_root)
    kept = [e for e in entries if e.get("job_id") != job_id]
    changed = len(kept) != len(entries)
    if changed:
        _save_index(kept, upload_root)
    if delete_files:
        job_dir = _job_dir(job_id, upload_root)
        if job_dir.is_dir():
            shutil.rmtree(job_dir)
    return {"evicted": changed}


def list_history(upload_root=None):
    """Return the indexed jobs newest-first with live per-job metadata.

    Each entry carries the job_config (or the pre-F9 defaults), the voice
    source, the source filename and the current status stage/state. Missing
    job dirs (files already deleted) are skipped. Never raises.
    """
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    out = []
    for entry in _load_index(root):
        job_id = entry.get("job_id")
        if not _job_dir(job_id, root).is_dir():
            continue
        cfg = job_config.read_config(job_id, root) or {}
        meta = _load_job_meta(job_id, root)
        status = job_status.read_status(job_id, root)
        voice_source = cfg.get("voice_source") or voiceover_unify.get_voice_source(
            job_id, root
        )
        out.append(
            {
                "job_id": job_id,
                "created_at": entry.get("created_at"),
                "target_video_name": (
                    entry.get("target_video_name")
                    or meta.get("source_filename")
                    or job_id
                ),
                "engine": cfg.get("engine"),
                "source_lang": cfg.get("source_lang"),
                "target_lang": cfg.get("target_lang"),
                "voice_source": voice_source,
                "stage": status.get("stage"),
                "state": status.get("state"),
            }
        )
    return out
