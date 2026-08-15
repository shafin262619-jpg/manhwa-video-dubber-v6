"""Independent local-Whisper cross-check for Chinese subtitle extraction (D1).

Extraction (B1, subtitle_extract.py) relies entirely on Gemini video
understanding. This module runs a lightweight, local, audio-only
double-check using Whisper so a large Gemini extraction failure (missing
dialogue block, hallucinated content) can be flagged even without a human
manually reading the SRT. Never treated as ground truth, never replaces
Gemini's output -- purely a coverage/sanity signal.
"""

import json
import logging
from pathlib import Path

from pipeline import config, job_logging, subtitle_builder, video_ingest
from pipeline.voiceover_upload import _convert_to_wav

logger = logging.getLogger(__name__)

CROSS_CHECK_WAV_NAME = "whisper_cross_check.wav"
OUTPUT_JSON_NAME = "subtitle_qa_whisper.json"


def _write_result(job_dir, result):
    """Best-effort persist of the cross-check result dict. Never raises."""
    try:
        (job_dir / OUTPUT_JSON_NAME).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _skipped_result(reason, covered_sec):
    return {
        "status": "skipped",
        "reason": reason,
        "whisper_spoken_sec": 0.0,
        "extracted_covered_sec": round(float(covered_sec), 3),
        "coverage_ratio": None,
        "mismatch": False,
    }


def whisper_cross_check(job_id, upload_root=None, logger_=None):
    """Extract audio from ``source.mp4`` (ffmpeg, mono wav at
    ``config.TTS_SAMPLE_RATE``, same pattern as
    ``voiceover_upload._convert_to_wav()``), transcribe it with
    ``config.WHISPER_MODEL`` (segment-level, language auto-detect), and
    compare the Whisper-measured spoken duration against the Gemini-extracted
    covered duration from ``subtitle_qa.json`` (A3).

    Returns::

        {
            "status": "ok" | "skipped" | "mismatch",
            "reason": None | "whisper_not_installed" | "transcription_failed",
            "whisper_spoken_sec": <total duration of all whisper segments>,
            "extracted_covered_sec": <covered_duration_sec from subtitle_qa.json>,
            "coverage_ratio": extracted_covered_sec / whisper_spoken_sec
                               (None when whisper_spoken_sec is zero),
            "mismatch": bool,   # True when the ratio is below
                                # config.SUBTITLE_COVERAGE_MISMATCH_RATIO
        }

    Whisper missing or transcription failing (import/runtime error) returns
    ``{"status": "skipped", "reason": ...}`` instead of raising. This
    function never raises. The result is also written to
    ``uploads/<job_id>/subtitle_qa_whisper.json``.
    """
    log = logger_ or logger
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    source_path = job_dir / "source.mp4"

    qa = subtitle_builder.load_subtitle_qa(job_id, upload_root=upload_root)
    try:
        covered_sec = float(qa.get("covered_duration_sec", 0.0))
    except (TypeError, ValueError):
        covered_sec = 0.0

    if not source_path.exists():
        log.warning("job %s: no source.mp4; skipping whisper cross-check", job_id)
        result = _skipped_result("transcription_failed", covered_sec)
        _write_result(job_dir, result)
        return result

    try:
        wav_path = job_dir / CROSS_CHECK_WAV_NAME
        _convert_to_wav(source_path, wav_path)
    except Exception as exc:  # noqa: BLE001 - never raise
        log.error("job %s: whisper cross-check audio extraction failed: %s", job_id, exc)
        result = _skipped_result("transcription_failed", covered_sec)
        _write_result(job_dir, result)
        return result

    try:
        import whisper  # lazy: heavy optional dependency
    except ImportError as exc:
        log.error("job %s: whisper not installed; skipping cross-check: %s", job_id, exc)
        result = _skipped_result("whisper_not_installed", covered_sec)
        _write_result(job_dir, result)
        return result

    try:
        model = whisper.load_model(config.WHISPER_MODEL)
        transcribed = model.transcribe(str(wav_path))
    except Exception as exc:  # noqa: BLE001 - never raise
        log.error("job %s: whisper transcription failed: %s", job_id, exc)
        result = _skipped_result("transcription_failed", covered_sec)
        _write_result(job_dir, result)
        return result

    spoken_sec = 0.0
    for segment in (transcribed.get("segments") or []):
        if not isinstance(segment, dict):
            continue
        try:
            start_sec = float(segment.get("start", 0.0))
            end_sec = float(segment.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        spoken_sec += max(end_sec - start_sec, 0.0)

    if spoken_sec > 0:
        coverage_ratio = covered_sec / spoken_sec
    else:
        coverage_ratio = None

    mismatch = (
        coverage_ratio is not None
        and coverage_ratio < config.SUBTITLE_COVERAGE_MISMATCH_RATIO
    )
    status = "mismatch" if mismatch else "ok"

    result = {
        "status": status,
        "reason": None,
        "whisper_spoken_sec": round(spoken_sec, 3),
        "extracted_covered_sec": round(covered_sec, 3),
        "coverage_ratio": (
            round(coverage_ratio, 4) if coverage_ratio is not None else None
        ),
        "mismatch": mismatch,
    }
    log.info(
        "job %s: whisper cross-check %s (covered %.3f / spoken %.3f = %s)",
        job_id, status, covered_sec, spoken_sec,
        "n/a" if coverage_ratio is None else f"{coverage_ratio:.4f}",
    )
    _write_result(job_dir, result)
    return result
