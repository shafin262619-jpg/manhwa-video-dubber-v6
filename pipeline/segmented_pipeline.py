"""Per-segment pipeline orchestrator (F13b).

Runs the downstream chain independently on each transcript-gap segment,
sequentially, so segment N is fully rendered before segment N+1 starts and
the job-status file exposes per-segment progress (a client can see segment 1
done before segment 2 begins).

For each segment (in order):

1. ``segmentation.materialize_segment`` — cut ``source.mp4`` and write the
   segment-relative ``subtitles_zh_raw.json`` slice + ``job_meta.json``.
2. upload chain: B2 (``subtitle_builder.build_subtitle_list``) -> whisper
   cross-check (``subtitle_verify.whisper_cross_check``) -> C1
   (``translator.translate_subtitles``), each against the segment's mini-job
   directory.
3. voiceover chain (auto_tts): D2 -> D4 -> E1 -> E2 -> F3, per segment.
4. ``job_status.mark_segment_done`` — per-segment completion + final path.

Single-segment plans are NOT handled here: the caller keeps the existing
whole-video path for exactly-one-segment jobs, so short videos behave
byte-identically to today.

The user_upload continuation — merge the per-segment C1 lists, align the
uploaded audio globally once, slice it per segment, then render
D4 -> E1 -> E2 -> F3 per segment — is provided by
:func:`run_segmented_user_audio_pipeline`, invoked from the app once the
user's audio is uploaded.
"""

import json
import logging
import subprocess
from pathlib import Path

from pipeline import (
    auto_cut,
    edit_guideline,
    job_logging,
    job_status as job_status_store,
    lang_files,
    render_final,
    segmentation,
    subtitle_builder,
    subtitle_verify,
    translator,
    video_ingest,
    voiceover_auto,
    voiceover_unify,
    voiceover_upload,
)

logger = logging.getLogger(__name__)

# Sub-directory under ``outputs/<job_id>/`` holding per-segment final videos.
SEGMENT_OUTPUT_DIR = "segments"

# Per-segment upload-chain stage names (job_status ``segments`` map keys).
SEGMENT_UPLOAD_STAGES = (
    "B2_subtitles",
    "whisper_cross_check",
    "C1_translate",
)


def segment_final_path(job_id, seg_index, output_root=None):
    """Destination of a segment's standalone final video (does not render)."""
    if output_root is None:
        output_root = render_final.OUTPUT_ROOT
    key = segmentation.segment_key(seg_index)
    return Path(output_root) / job_id / SEGMENT_OUTPUT_DIR / key / "final_video.mp4"


def _run_segment_stage(job_id, seg_index, stage, func, *args, **kwargs):
    """Run a per-segment stage with per-segment running/done/error status.

    Mirrors ``job_status.run_stage`` scoped to one segment: writes the
    segment's ``stages[stage]`` transition around ``func(*args, **kwargs)``.
    Status writes are best-effort; the stage result/error always wins.
    """
    try:
        job_status_store.write_segment_status(job_id, seg_index, stage, "running")
    except Exception:  # noqa: BLE001 - status is advisory
        pass
    try:
        result = func(*args, **kwargs)
    except Exception:
        try:
            job_status_store.write_segment_status(job_id, seg_index, stage, "error")
        except Exception:  # noqa: BLE001
            pass
        raise
    try:
        job_status_store.write_segment_status(job_id, seg_index, stage, "done")
    except Exception:  # noqa: BLE001
        pass
    return result


def run_segment_upload_chain(job_id, segment, seg_dir, upload_root, call_budget,
                             logger_=None):
    """B2 -> whisper cross-check -> C1 for one segment. Raises on failure.

    Each stage is skipped when its artifact already exists in the segment dir,
    so a resumed pipeline never re-runs completed per-segment work.
    """
    log = logger_ or logger
    lang = lang_files.target_lang(job_id, upload_root)
    if not (seg_dir / "subtitles_zh.json").exists():
        log.info("job %s seg %d: B2 build", job_id, segment["index"])
        _run_segment_stage(
            job_id, segment["index"], "B2_subtitles",
            subtitle_builder.build_subtitle_list, job_id,
            upload_root=upload_root, call_budget=call_budget, job_dir=seg_dir,
            time_offset_sec=segment["start_sec"],
        )
    if not (seg_dir / "subtitle_qa_whisper.json").exists():
        log.info("job %s seg %d: whisper cross-check", job_id, segment["index"])
        _run_segment_stage(
            job_id, segment["index"], "whisper_cross_check",
            subtitle_verify.whisper_cross_check, job_id,
            upload_root=upload_root, logger_=log, job_dir=seg_dir,
        )
    if not (seg_dir / lang_files.subtitles_json(lang)).exists():
        log.info("job %s seg %d: C1 translate", job_id, segment["index"])
        _run_segment_stage(
            job_id, segment["index"], "C1_translate",
            translator.translate_subtitles, job_id,
            upload_root=upload_root, call_budget=call_budget, job_dir=seg_dir,
        )


def run_segment_voiceover_chain(job_id, segment, seg_dir, upload_root,
                                call_budget, logger_=None):
    """auto_tts D2 -> D4 -> E1 -> E2 -> F3 for one segment.

    Raises on failure; returns the segment's final video path.
    """
    log = logger_ or logger
    index = segment["index"]
    log.info("job %s seg %d: D2 auto voiceover", job_id, index)
    _run_segment_stage(
        job_id, index, "D2_voiceover",
        voiceover_auto.generate_auto_voiceover, job_id,
        upload_root=upload_root, call_budget=call_budget, job_dir=seg_dir,
    )
    _run_segment_stage(
        job_id, index, "D4_unify",
        voiceover_unify.unify_voiceover_timestamps, job_id,
        upload_root=upload_root, job_dir=seg_dir,
    )
    _run_segment_stage(
        job_id, index, "E1_guideline",
        edit_guideline.build_edit_guideline, job_id,
        upload_root=upload_root, job_dir=seg_dir,
    )
    _run_segment_stage(
        job_id, index, "E2_draft",
        auto_cut.build_draft_video, job_id,
        upload_root=upload_root, job_dir=seg_dir,
    )
    final_path = segment_final_path(job_id, index)
    log.info("job %s seg %d: F3 final render", job_id, index)
    _run_segment_stage(
        job_id, index, "F3_final",
        render_final.finalize_video, job_id,
        upload_root=upload_root, job_dir=seg_dir, output_path=final_path,
    )
    return final_path


def run_segmented_pipeline(job_id, upload_root=None, call_budget=None):
    """Run the whole downstream chain per segment, sequentially (F13b).

    Loads the persisted segment plan, materialises + processes each segment
    in order and records per-segment completion in the job-status file.
    ``auto_tts`` jobs continue straight through D2 -> F3 per segment; other
    voice sources stop after per-segment C1 (the user_upload continuation is
    triggered once the audio arrives).

    Raises on the first failing segment so the caller can persist the error.
    """
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    plan = segmentation.load_plan(job_id, upload_root)
    log = job_logging.get_job_logger(job_id, upload_root)
    mode = voiceover_unify.get_voice_source(job_id, upload_root)

    segments_results = []
    for segment in plan["segments"]:
        index = segment["index"]
        log.info("job %s: processing segment %d [%.3f, %.3f)",
                 job_id, index, segment["start_sec"], segment["end_sec"])
        try:
            seg_dir = segmentation.materialize_segment(job_id, plan, segment, upload_root)
            run_segment_upload_chain(
                job_id, segment, seg_dir, root, call_budget, logger_=log
            )
            final_path = None
            if mode == "auto_tts":
                final_path = run_segment_voiceover_chain(
                    job_id, segment, seg_dir, root, call_budget, logger_=log
                )
        except Exception as exc:  # noqa: BLE001 - persist + re-raise
            try:
                job_status_store.mark_segment_error(
                    job_id, index, message=str(exc)
                )
            except Exception:  # noqa: BLE001 - status is advisory
                pass
            raise
        try:
            job_status_store.mark_segment_done(
                job_id, index, final_path=final_path,
                extra={"status": "ok", "entries_count": segment["entries_count"]},
            )
        except Exception:  # noqa: BLE001 - status is advisory
            pass
        segments_results.append(
            {
                "index": index,
                "start_sec": segment["start_sec"],
                "end_sec": segment["end_sec"],
                "entries_count": segment["entries_count"],
                "final_path": str(final_path) if final_path else None,
                "status": "ok",
            }
        )
    return {"segmented": True, "mode": mode, "segments": segments_results}


# ---------------------------------------------------------------------------
# user_upload continuation (F13b)
# ---------------------------------------------------------------------------

def _slice_audio(audio_path, start_sec, end_sec, out_path):
    """Cut ``[start_sec, end_sec)`` from an audio file (``-c copy``).

    Audio frames are individually seekable, so this is sample-accurate for the
    pipeline's normalized wav voiceovers (same ``-ss``/``-to``/``-c copy``
    pattern as ``subtitle_extract``'s segment cuts).
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{start_sec:.3f}", "-to", f"{end_sec:.3f}",
                "-i", str(audio_path),
                "-c", "copy",
                str(out_path),
            ],
            capture_output=True, text=True, timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"ffmpeg failed: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {result.stderr.strip()}")


def _merge_segment_translations(job_id, plan, upload_root):
    """Merge per-segment ``subtitles_<lang>.json`` into one list with fresh
    global serials. Returns ``(merged, seg_meta)`` where ``seg_meta`` maps
    segment index -> (first_global_serial, count)."""
    lang = lang_files.target_lang(job_id, upload_root)
    sub_name = lang_files.subtitles_json(lang)
    merged = []
    seg_meta = {}
    for segment in plan["segments"]:
        seg_dir = segmentation.segment_dir(job_id, segment["index"], upload_root)
        path = seg_dir / sub_name
        if not path.exists():
            raise FileNotFoundError(
                f"no {sub_name} for segment {segment['index']} of job {job_id}"
            )
        entries = json.loads(path.read_text(encoding="utf-8"))
        first = len(merged) + 1
        for pos, entry in enumerate(entries):
            e = dict(entry)
            e["serial"] = first + pos
            merged.append(e)
        seg_meta[segment["index"]] = (first, len(entries))
    return merged, seg_meta


def run_segmented_user_audio_pipeline(job_id, upload_root=None):
    """user_upload continuation for a segmented job.

    The user's audio covers the whole video, so the voiceover is aligned once
    against the merged per-segment translations, then sliced per segment and
    D4 -> E1 -> E2 -> F3 is rendered per segment. Raises on failure.

    Returns a summary dict with per-segment final paths.
    """
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = root / job_id
    plan = segmentation.load_plan(job_id, upload_root)
    lang = lang_files.target_lang(job_id, upload_root)
    sub_name = lang_files.subtitles_json(lang)
    ts_name = lang_files.timestamps_upload(lang)
    audio_name = lang_files.voiceover_audio(lang)
    log = job_logging.get_job_logger(job_id, upload_root)

    merged, seg_meta = _merge_segment_translations(job_id, plan, upload_root)
    (job_dir / sub_name).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log.info("job %s: global D3 alignment of uploaded voiceover", job_id)
    voiceover_upload.align_uploaded_voiceover(job_id, upload_root=upload_root)
    ts_all = json.loads((job_dir / ts_name).read_text(encoding="utf-8"))
    ts_by_serial = {
        int(entry.get("serial")): entry
        for entry in ts_all
        if isinstance(entry, dict)
    }

    audio_all = job_dir / audio_name
    if not audio_all.exists():
        raise FileNotFoundError(f"no {audio_name} for job {job_id}")

    for segment in plan["segments"]:
        index = segment["index"]
        seg_dir = segmentation.segment_dir(job_id, index, upload_root)
        first_global, count = seg_meta[index]
        global_serials = range(first_global, first_global + count)
        seg_ts = [ts_by_serial[s] for s in global_serials if s in ts_by_serial]
        if not seg_ts:
            raise voiceover_unify.VoiceoverAlignmentError(
                f"job {job_id} segment {index}: no aligned voiceover timestamps "
                "for its subtitle lines. Re-upload a valid audio file."
            )
        audio_start = min(float(entry["start_sec"]) for entry in seg_ts)
        audio_end = max(float(entry["end_sec"]) for entry in seg_ts)
        slice_path = seg_dir / audio_name
        _slice_audio(audio_all, audio_start, audio_end, slice_path)

        local_ts = []
        for s in global_serials:
            if s in ts_by_serial:
                entry = dict(ts_by_serial[s])
                entry["serial"] = s - first_global + 1
                entry["start_sec"] = round(float(entry["start_sec"]) - audio_start, 3)
                entry["end_sec"] = round(float(entry["end_sec"]) - audio_start, 3)
                local_ts.append(entry)
        (seg_dir / ts_name).write_text(
            json.dumps(local_ts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("job %s seg %d: sliced voiceover audio [%.3f, %.3f]",
                 job_id, index, audio_start, audio_end)

    segments_results = []
    for segment in plan["segments"]:
        index = segment["index"]
        seg_dir = segmentation.segment_dir(job_id, index, upload_root)
        _run_segment_stage(
            job_id, index, "D3_align",
            lambda: None,
        )
        _run_segment_stage(
            job_id, index, "D4_unify",
            voiceover_unify.unify_voiceover_timestamps, job_id,
            upload_root=upload_root, job_dir=seg_dir,
        )
        _run_segment_stage(
            job_id, index, "E1_guideline",
            edit_guideline.build_edit_guideline, job_id,
            upload_root=upload_root, job_dir=seg_dir,
        )
        _run_segment_stage(
            job_id, index, "E2_draft",
            auto_cut.build_draft_video, job_id,
            upload_root=upload_root, job_dir=seg_dir,
        )
        final_path = segment_final_path(job_id, index)
        _run_segment_stage(
            job_id, index, "F3_final",
            render_final.finalize_video, job_id,
            upload_root=upload_root, job_dir=seg_dir, output_path=final_path,
        )
        try:
            job_status_store.mark_segment_done(
                job_id, index, final_path=final_path,
                extra={"status": "ok"},
            )
        except Exception:  # noqa: BLE001 - status is advisory
            pass
        segments_results.append(
            {
                "index": index,
                "start_sec": segment["start_sec"],
                "end_sec": segment["end_sec"],
                "final_path": str(final_path),
                "status": "ok",
            }
        )
    return {"segmented": True, "mode": "user_upload", "segments": segments_results}
