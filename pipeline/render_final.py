"""Final render + download (F3).

Copies/normalizes the approved draft video into a final deliverable and the
app exposes it for download.

``finalize_video(job_id)`` reads ``uploads/<job_id>/draft_final_video.mp4``
(E2 draft / F2 re-spliced) and writes a normalized H.264/AAC mp4 to
``outputs/<job_id>/final_video.mp4``. The normalization always re-encodes so
any container/codec quirk in the draft is fixed and the file gets a
``+faststart`` moov atom for instant streaming — this mirrors the Auto Manhwa
Maker ``_ffmpeg_normalize`` deliverable step.

``app.py``:
- ``GET /final/{job_id}`` — runs the finalize and shows the final video with a
  download link and a "Back to Review" link back to ``/review/{job_id}``.
- ``GET /download/{job_id}`` — serves ``outputs/<job_id>/final_video.mp4``.
"""

import logging
from pathlib import Path

from pipeline import auto_cut, config, job_logging, video_ingest

logger = logging.getLogger(__name__)

OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "outputs"


def final_video_path(job_id, output_root=None):
    """Expected final deliverable path (does not render anything)."""
    output_root = Path(output_root) if output_root else OUTPUT_ROOT
    return output_root / job_id / "final_video.mp4"


def _normalize_command(src, dst):
    return [
        "ffmpeg", "-y",
        "-i", str(src),
        "-c:v", config.RENDER_VIDEO_CODEC,
        "-preset", config.RENDER_VIDEO_PRESET,
        "-pix_fmt", config.RENDER_PIX_FMT,
        "-c:a", config.RENDER_AUDIO_CODEC,
        "-movflags", "+faststart",
        str(dst),
    ]


def finalize_video(job_id, upload_root=None, output_root=None):
    """Copy/normalize the draft into ``outputs/<job_id>/final_video.mp4``.

    Returns a result dict with the final path and duration. Raises
    FileNotFoundError when the job or ``draft_final_video.mp4`` is missing;
    RuntimeError when the ffmpeg step fails or the output is not produced.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job not found: {job_id}")

    draft = job_dir / "draft_final_video.mp4"
    if not draft.exists():
        raise FileNotFoundError(f"no draft_final_video.mp4 for job {job_id}")

    job_logger = job_logging.get_job_logger(job_id, upload_root)
    out_path = final_video_path(job_id, output_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    auto_cut._run(_normalize_command(draft, out_path))
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"final render produced no output for job {job_id}")

    probe = auto_cut._probe(out_path)
    try:
        duration_sec = float(probe.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration_sec = None

    job_logger.info("job %s: final video written -> %s", job_id, out_path)
    return {
        "job_id": job_id,
        "status": "ok",
        "final_path": str(out_path),
        "draft_path": str(draft),
        "duration_sec": duration_sec,
    }
