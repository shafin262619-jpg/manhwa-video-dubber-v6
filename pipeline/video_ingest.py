"""Video ingest pipeline.

Validates uploaded video files, saves each upload under its own job directory
(``uploads/<job_id>/source.mp4``) and probes the video with ffprobe to build
``job_meta.json`` (duration / resolution), needed by later chunks (B1
chunking decisions).
"""

import json
import shutil
import subprocess
import uuid
from pathlib import Path

UPLOAD_ROOT = Path(__file__).resolve().parent.parent / "uploads"

ALLOWED_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v"}

NO_ACTIVE_KEY_MESSAGE = "Settings-এ গিয়ে আগে একটা Gemini API key যোগ করুন"


class NoActiveKeyError(Exception):
    """Raised when no active Gemini API key is configured."""


class UnsupportedFileError(Exception):
    """Raised when the uploaded file type is not supported."""


class VideoProbeError(Exception):
    """Raised when ffprobe cannot read the video metadata."""


def new_job_id():
    return str(uuid.uuid4())


def validate_file_type(filename):
    """Raise UnsupportedFileError unless the file extension is supported."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileError(
            f"Unsupported file type '{ext or '(none)'}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    return ext


def probe_video(path):
    """Return duration (sec) and resolution (width/height) via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise VideoProbeError(f"ffprobe failed: {exc}") from exc
    if result.returncode != 0:
        raise VideoProbeError(f"ffprobe error: {result.stderr.strip()}")
    data = json.loads(result.stdout)

    duration = None
    width = height = None
    if data.get("format", {}).get("duration"):
        duration = float(data["format"]["duration"])
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            width = stream.get("width")
            height = stream.get("height")
            if duration is None and stream.get("duration"):
                duration = float(stream["duration"])
            break
    return {"duration_sec": duration, "width": width, "height": height}


def ensure_active_key(active_keys):
    """Gate uploads on the presence of at least one active Gemini key."""
    if not active_keys:
        raise NoActiveKeyError(NO_ACTIVE_KEY_MESSAGE)


def finalize_job(job_id, original_filename, upload_root=None):
    """Probe the saved ``source.mp4`` and write ``job_meta.json``."""
    upload_root = Path(upload_root) if upload_root else UPLOAD_ROOT
    job_dir = upload_root / job_id
    dest = job_dir / "source.mp4"
    if not dest.exists():
        raise FileNotFoundError(f"no source.mp4 for job {job_id}")
    meta = probe_video(dest)
    job_meta = {
        "job_id": job_id,
        "source_filename": original_filename,
        "source_path": str(dest),
        "size_bytes": dest.stat().st_size,
        "duration_sec": meta["duration_sec"],
        "width": meta["width"],
        "height": meta["height"],
    }
    (job_dir / "job_meta.json").write_text(
        json.dumps(job_meta, indent=2), encoding="utf-8"
    )
    return job_meta


def create_job(source_path, original_filename, job_id=None, upload_root=None):
    """Copy an uploaded file into a new job dir and register its metadata."""
    validate_file_type(original_filename)
    upload_root = Path(upload_root) if upload_root else UPLOAD_ROOT
    job_id = job_id or new_job_id()
    job_dir = upload_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, job_dir / "source.mp4")
    return finalize_job(job_id, original_filename, upload_root)
