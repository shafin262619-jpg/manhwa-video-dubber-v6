"""Per-job persistent logging.

Every pipeline stage for a given job appends its progress messages to the same
file ``uploads/<job_id>/logs/pipeline.log`` so the whole job lifecycle can be
replayed later.  ``get_job_logger`` returns a ``logging.Logger`` that writes to
that file in append mode; calling it repeatedly for the same job does not add
duplicate file handlers.
"""

import logging
from pathlib import Path

from pipeline import video_ingest

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_LEVEL = logging.INFO


def get_job_logger(job_id, upload_root=None):
    """Return a per-job logger writing to ``uploads/<job_id>/logs/pipeline.log``.

    ``upload_root`` defaults to ``video_ingest.UPLOAD_ROOT``.  The log file is
    created (append mode) and the parent directories are created on demand.
    Repeated calls for the same job reuse the existing file handler instead of
    stacking new handlers.
    """
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    log_path = root / job_id / "logs" / "pipeline.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"manhwa.job.{job_id}")
    logger.setLevel(LOG_LEVEL)

    existing = {
        handler.baseFilename
        for handler in logger.handlers
        if isinstance(handler, logging.FileHandler)
    }
    if str(log_path) not in existing:
        handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    return logger
