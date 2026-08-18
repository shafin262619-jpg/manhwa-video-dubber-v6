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

from google import genai
from google.genai import types as genai_types

from pipeline import (
    auto_cut,
    config,
    edit_guideline,
    error_bn,
    job_logging,
    job_status as job_status_store,
    key_store,
    lang_files,
    render_final,
    segmentation,
    subtitle_builder,
    subtitle_extract,
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

    Each stage is skipped when its artifact already exists in the segment dir,
    so a resumed pipeline never re-runs completed per-segment work (mirrors
    ``run_segment_upload_chain``). On a fresh segment no artifact exists yet,
    so every stage still runs — behavior is byte-identical to a first pass.
    Raises on failure; returns the segment's final video path.
    """
    log = logger_ or logger
    index = segment["index"]
    lang = lang_files.target_lang(job_id, upload_root)
    if not (seg_dir / lang_files.timestamps_auto(lang)).exists():
        log.info("job %s seg %d: D2 auto voiceover", job_id, index)
        _run_segment_stage(
            job_id, index, "D2_voiceover",
            voiceover_auto.generate_auto_voiceover, job_id,
            upload_root=upload_root, call_budget=call_budget, job_dir=seg_dir,
        )
    if not (seg_dir / lang_files.timestamps_final(lang)).exists():
        _run_segment_stage(
            job_id, index, "D4_unify",
            voiceover_unify.unify_voiceover_timestamps, job_id,
            upload_root=upload_root, job_dir=seg_dir,
        )
    if not (seg_dir / "edit_guideline.json").exists():
        _run_segment_stage(
            job_id, index, "E1_guideline",
            edit_guideline.build_edit_guideline, job_id,
            upload_root=upload_root, job_dir=seg_dir,
        )
    if not (seg_dir / "draft_final_video.mp4").exists():
        _run_segment_stage(
            job_id, index, "E2_draft",
            auto_cut.build_draft_video, job_id,
            upload_root=upload_root, job_dir=seg_dir,
        )
    final_path = segment_final_path(job_id, index)
    if not final_path.exists():
        log.info("job %s seg %d: F3 final render", job_id, index)
        _run_segment_stage(
            job_id, index, "F3_final",
            render_final.finalize_video, job_id,
            upload_root=upload_root, job_dir=seg_dir, output_path=final_path,
        )
    return final_path


def run_segmented_pipeline(job_id, upload_root=None, call_budget=None,
                           start_segment_index=0):
    """Run the whole downstream chain per segment, sequentially (F13b).

    Loads the persisted segment plan, materialises + processes each segment
    in order and records per-segment completion in the job-status file.
    ``auto_tts`` jobs continue straight through D2 -> F3 per segment; other
    voice sources stop after per-segment C1 (the user_upload continuation is
    triggered once the audio arrives).

    ``start_segment_index`` (F13b Part C) re-enters the loop at a specific
    segment instead of the first one — the resume path continues from the
    first incomplete segment, never re-processing completed ones. Segments
    already marked ``done`` in job status are skipped outright (idempotent
    re-entry) so a repeated resume can never duplicate work.

    Raises on the first failing segment so the caller can persist the error.
    """
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    plan = segmentation.load_plan(job_id, upload_root)
    log = job_logging.get_job_logger(job_id, upload_root)
    mode = voiceover_unify.get_voice_source(job_id, upload_root)

    segments_results = []
    for segment in plan["segments"]:
        index = segment["index"]
        entry = job_status_store.read_segment_status(job_id, index, upload_root)
        if index < start_segment_index:
            if entry.get("state") == "done":
                segments_results.append(
                    {
                        "index": index,
                        "start_sec": segment["start_sec"],
                        "end_sec": segment["end_sec"],
                        "entries_count": segment["entries_count"],
                        "final_path": entry.get("final_path"),
                        "status": "ok",
                    }
                )
            continue
        if entry.get("state") == "done":
            log.info("job %s: segment %d already complete — skipping",
                     job_id, index)
            segments_results.append(
                {
                    "index": index,
                    "start_sec": segment["start_sec"],
                    "end_sec": segment["end_sec"],
                    "entries_count": segment["entries_count"],
                    "final_path": entry.get("final_path"),
                    "status": "ok",
                }
            )
            continue
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
        if final_path is not None:
            try:
                run_auto_qa_gate(
                    job_id, segment, seg_dir, final_path,
                    upload_root=root, call_budget=call_budget, logger_=log,
                )
            except Exception as exc:  # noqa: BLE001 - never block the segment
                log.error("job %s seg %d: auto QA gate raised: %s",
                          job_id, index, exc)
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
            run_auto_qa_gate(
                job_id, segment, seg_dir, final_path,
                upload_root=root, call_budget=None, logger_=log,
            )
        except Exception as exc:  # noqa: BLE001 - never block the segment
            log.error("job %s seg %d: auto QA gate raised: %s",
                      job_id, index, exc)
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


# ---------------------------------------------------------------------------
# Targeted per-segment correction re-run (F14b Part 1)
#
# When a reviewer flags a completed segment's output, the fix is NOT a blind
# full re-run: only the pipeline stage that owns the reported problem is
# re-run (fed its own previous output plus a clear correction instruction),
# then every downstream stage that consumes the corrected stage's output is
# mechanically re-run in order. Stages upstream of or independent from the
# corrected stage are never re-run, and no other segment is touched.
# ---------------------------------------------------------------------------

# The per-segment stage order (job_status ``segments`` map keys).
SEGMENT_STAGE_ORDER = (
    "B2_subtitles",
    "whisper_cross_check",
    "C1_translate",
    "D2_voiceover",
    "D4_unify",
    "E1_guideline",
    "E2_draft",
    "F3_final",
)

# Per-segment runnable stages by voice source. D3 (user upload alignment) is a
# whole-video operation, so it is never part of a per-segment re-run.
MODE_SEGMENT_STAGES = {
    "auto_tts": SEGMENT_STAGE_ORDER,
    "user_upload": (
        "B2_subtitles",
        "whisper_cross_check",
        "C1_translate",
        "D4_unify",
        "E1_guideline",
        "E2_draft",
        "F3_final",
    ),
}

# Issue tag -> owning stage. Multi-tag rounds target the EARLIEST mapped stage
# in pipeline order: correcting it cascades to every later stage anyway.
ISSUE_TAG_TO_STAGE = {
    "bad_translation": "C1_translate",
    "subtitle_timing": "B2_subtitles",
    "tts_quality": "D2_voiceover",
    "timing_mismatch": "D4_unify",
    "audio_glitch": "E2_draft",
    # Free-text-only complaints have no single owning stage; re-running from
    # translation forward is the safest broad net (covers every artifact a
    # human can review) without touching the out-of-scope F1 whole-video
    # extraction or F13 boundary logic.
    "other": "C1_translate",
}

# F14b Part 1 re-run fallback stage when a mapped stage is not runnable in the
# current voice-source mode (e.g. ``tts_quality`` on a ``user_upload`` job has
# no auto-TTS to regenerate).
RERUN_FALLBACK_STAGE = "C1_translate"

# Map a stage key to the artifact that stage itself produced last time, for
# the correction instruction's "previous output" section.
def _previous_output_name(stage_key, job_id, upload_root):
    lang = lang_files.target_lang(job_id, upload_root)
    return {
        "B2_subtitles": "subtitles_zh.json",
        "whisper_cross_check": "subtitle_qa_whisper.json",
        "C1_translate": lang_files.subtitles_json(lang),
        "D2_voiceover": lang_files.timestamps_auto(lang),
        "D4_unify": lang_files.timestamps_final(lang),
        "E1_guideline": "edit_guideline.json",
        "E2_draft": "draft_final_video.mp4",
        "F3_final": "final_video.mp4",
    }.get(stage_key)


def _load_stage_previous_output(job_id, seg_dir, stage_key, upload_root):
    """Load the target stage's own previous output for this segment as text.

    Returns a short human/LLM-readable summary of whatever artifact the stage
    produced last time (its translated text, TTS timestamps, aligned
    timestamps, guideline, etc.). Binary media artifacts are summarised by
    path/size rather than dumped raw.
    """
    name = _previous_output_name(stage_key, job_id, upload_root)
    if name is None:
        return "(no recorded artifact for this stage)"
    if stage_key in ("E2_draft", "F3_final"):
        if stage_key == "E2_draft":
            path = seg_dir / "draft_final_video.mp4"
        else:
            path = segment_final_path(job_id, _segment_index_of(seg_dir))
        if not path.exists():
            return f"(no previous {name} on disk)"
        return f"{name}: binary media file, {path.stat().st_size} bytes"
    path = seg_dir / name
    if not path.exists():
        return f"(no previous {name} on disk)"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"{name}: unreadable artifact"
    text = text.strip()
    if len(text) > 4000:
        text = text[:4000] + "\n[truncated]"
    return text


def _segment_index_of(seg_dir):
    name = Path(seg_dir).name
    return int(name.rsplit("_", 1)[1]) if "_" in name else 0


def build_correction_instruction(issues, labels_bn, notes, previous_output):
    """Assemble the explicit correction instruction fed to a re-run stage.

    Combines the Bengali issue-category labels, the reviewer's free text and
    the stage's own previous output, framed so the underlying model call is
    told plainly what was wrong and what to fix while preserving the parts of
    the previous output that were not flagged.
    """
    parts = [
        "CORRECTION INSTRUCTION: the previous output of this segment was "
        "reviewed. Fix ONLY the problem(s) reported below and keep every part "
        "of the previous output that was not flagged.",
        "Reported issue(s):",
    ]
    parts.extend(f"- {label}" for label in (labels_bn or []))
    parts.append(
        "Reviewer notes: " + (str(notes).strip() if notes else "(none)")
    )
    parts.append("Previous output (fix only the flagged parts):")
    parts.append(str(previous_output or "(none)"))
    return "\n".join(parts)


def _invoke_rerun_stage(stage, job_id, seg_dir, root, call_budget, log,
                        segment, correction_hint):
    """Invoke one re-run stage against the segment's mini-job directory.

    ``correction_hint`` is forwarded only to the correction-capable owning
    stages (B2/C1/D2); downstream stages re-run mechanically on the corrected
    input. Raises on failure — the caller decides how to record it.
    """
    if stage == "B2_subtitles":
        return subtitle_builder.build_subtitle_list(
            job_id, upload_root=root, call_budget=call_budget,
            job_dir=seg_dir, time_offset_sec=segment["start_sec"],
            correction_hint=correction_hint,
        )
    if stage == "whisper_cross_check":
        return subtitle_verify.whisper_cross_check(
            job_id, upload_root=root, logger_=log, job_dir=seg_dir
        )
    if stage == "C1_translate":
        return translator.translate_subtitles(
            job_id, upload_root=root, call_budget=call_budget,
            job_dir=seg_dir, correction_hint=correction_hint,
        )
    if stage == "D2_voiceover":
        return voiceover_auto.generate_auto_voiceover(
            job_id, upload_root=root, call_budget=call_budget,
            job_dir=seg_dir, correction_hint=correction_hint,
        )
    if stage == "D4_unify":
        return voiceover_unify.unify_voiceover_timestamps(
            job_id, upload_root=root, job_dir=seg_dir
        )
    if stage == "E1_guideline":
        return edit_guideline.build_edit_guideline(
            job_id, upload_root=root, job_dir=seg_dir
        )
    if stage == "E2_draft":
        return auto_cut.build_draft_video(
            job_id, upload_root=root, job_dir=seg_dir
        )
    if stage == "F3_final":
        return render_final.finalize_video(
            job_id, upload_root=root, job_dir=seg_dir,
            output_path=segment_final_path(job_id, segment["index"]),
        )
    raise ValueError(f"unknown rerun stage {stage!r}")


def _stages_from(target, runnable):
    """Runnable stages at or downstream of ``target``, in pipeline order."""
    target_pos = SEGMENT_STAGE_ORDER.index(target)
    return [
        stage for stage in runnable
        if SEGMENT_STAGE_ORDER.index(stage) >= target_pos
    ]


def rerun_segment_stage(job_id, seg_index, round_no=None, review=None,
                        upload_root=None, call_budget=None, logger_=None):
    """Targeted re-run of the owning stage for a segment's review round (F14b).

    Given a completed segment and one of its recorded review rounds, this
    re-runs ONLY the pipeline stage that owns the reported problem — fed its
    own previous output plus a correction instruction — and then mechanically
    re-runs every downstream stage that consumes the corrected stage's output.
    No stage upstream of or independent from the corrected stage is re-run and
    no other segment is touched. One call produces one new review round (the
    triggering round + 1) via ``job_status.record_segment_rerun``; it never
    auto-loops.

    ``round_no`` selects the review round to correct (default: the most recent
    round that reported issues); ``review`` overrides the loaded round entry.
    Raises ValueError when the segment has no review for that round or the
    round reported no issues; FileNotFoundError when the job/segment/plan or
    the segment's mini-job directory is missing.

    A failed correction attempt never crashes the job: the attempt is recorded
    as a failed rerun round (Bengali ``rerun_error_bn``), the segment is rolled
    back to its last-good round's state, and a result dict is returned so a
    later UI chunk can offer retry/continue.
    """
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    log = logger_ or job_logging.get_job_logger(job_id, upload_root)
    plan = segmentation.load_plan(job_id, root)
    segment = next(
        (s for s in plan.get("segments") or [] if int(s.get("index")) == int(seg_index)),
        None,
    )
    if segment is None:
        raise FileNotFoundError(
            f"no segment {seg_index} in plan for job {job_id}"
        )

    if review is None:
        if round_no is None:
            reviews = job_status_store.get_segment_reviews(
                job_id, seg_index, upload_root=root
            )
            flagged = [
                (int(key), entry)
                for key, entry in reviews.items()
                if str(key).isdigit() and isinstance(entry, dict)
                and entry.get("issues")
            ]
            if not flagged:
                raise ValueError(
                    f"segment {seg_index} of job {job_id} has no review "
                    "with reported issues to correct"
                )
            round_no = max(candidate for candidate, _ in flagged)
            review = next(
                dict(entry) for candidate, entry in flagged
                if candidate == round_no
            )
        else:
            review = job_status_store.get_segment_reviews(
                job_id, seg_index, round_no=round_no, upload_root=root
            )
    if not isinstance(review, dict):
        raise ValueError(
            f"no review for segment {seg_index} of job {job_id} "
            f"(round {round_no})"
        )
    issues = [t for t in (review.get("issues") or []) if t in ISSUE_TAG_TO_STAGE]
    if not issues:
        raise ValueError(
            f"review round {review.get('round', round_no)} for segment "
            f"{seg_index} reported no issues"
        )

    triggered_round = int(review.get("round") or round_no or 1)
    mode = voiceover_unify.get_voice_source(job_id, root)
    runnable = MODE_SEGMENT_STAGES.get(mode, SEGMENT_STAGE_ORDER)

    mapped = [ISSUE_TAG_TO_STAGE[tag] for tag in issues]
    candidates = [stage for stage in runnable if stage in mapped]
    if not candidates:
        candidates = [stage for stage in runnable if stage == RERUN_FALLBACK_STAGE]
    if not candidates:
        raise ValueError(
            f"no runnable owning stage for issues {issues} in mode {mode!r}"
        )
    target = min(candidates, key=lambda stage: SEGMENT_STAGE_ORDER.index(stage))
    order = _stages_from(target, runnable)

    seg_dir = segmentation.segment_dir(job_id, seg_index, root)
    if not seg_dir.is_dir():
        raise FileNotFoundError(f"no segment mini-job dir for {seg_index}: {seg_dir}")

    labels_bn = [
        job_status_store.SEGMENT_REVIEW_ISSUE_CATEGORIES[tag]
        for tag in issues if tag in job_status_store.SEGMENT_REVIEW_ISSUE_CATEGORIES
    ]
    previous = _load_stage_previous_output(job_id, seg_dir, target, root)
    correction = build_correction_instruction(
        issues, labels_bn, review.get("notes"), previous
    )

    prior = job_status_store.read_segment_status(job_id, seg_index, upload_root=root)
    log.info(
        "job %s seg %d: targeted re-run for round %d -> target stage %s, "
        "stages %s", job_id, seg_index, triggered_round, target, list(order),
    )
    ran = []
    try:
        for stage in order:
            log.info("job %s seg %d: re-running stage %s", job_id, seg_index, stage)
            _run_segment_stage(
                job_id, seg_index, stage, _invoke_rerun_stage,
                stage, job_id, seg_dir, root, call_budget, log, segment,
                correction if stage == target else None,
            )
            ran.append(stage)
    except Exception as exc:  # noqa: BLE001 - persist + rollback, never crash
        bn = error_bn.explain_bn(exc, stage=target)
        log.error(
            "job %s seg %d: correction re-run failed at %s: %s",
            job_id, seg_index, target, exc,
        )
        try:
            job_status_store.restore_segment_state(
                job_id, seg_index, prior, upload_root=root
            )
        except Exception:  # noqa: BLE001 - status is advisory
            pass
        try:
            job_status_store.record_segment_rerun(
                job_id, seg_index, triggered_by_round=triggered_round,
                issues=issues, target_stage=target,
                status=job_status_store.SEGMENT_RERUN_FAILED,
                error_message=bn, correction=correction, upload_root=root,
            )
        except Exception:  # noqa: BLE001 - status is advisory
            pass
        return {
            "rerun": True,
            "status": "failed",
            "seg_index": int(seg_index),
            "round": triggered_round,
            "target_stage": target,
            "stages_rerun": ran,
            "error_bn": bn,
            "correction": correction,
        }

    new_round = None
    try:
        new_round = job_status_store.record_segment_rerun(
            job_id, seg_index, triggered_by_round=triggered_round,
            issues=issues, target_stage=target,
            status=job_status_store.SEGMENT_RERUN_OK,
            correction=correction, upload_root=root,
        )
    except Exception as exc:  # noqa: BLE001 - status is advisory
        log.error("job %s seg %d: failed to record rerun round: %s",
                  job_id, seg_index, exc)
    try:
        job_status_store.mark_segment_done(
            job_id, seg_index,
            final_path=str(segment_final_path(job_id, seg_index)),
            extra={"status": "ok", "entries_count": segment.get("entries_count")},
            upload_root=root,
        )
    except Exception:  # noqa: BLE001 - status is advisory
        pass
    return {
        "rerun": True,
        "status": "ok",
        "seg_index": int(seg_index),
        "round": new_round,
        "rerun_of_round": triggered_round,
        "target_stage": target,
        "stages_rerun": order,
        "correction": correction,
    }


# ---------------------------------------------------------------------------
# Automated pre-review QA gate (F14b Part 2)
#
# Before a finished segment is released to human review, Gemini watches the
# segment's final video once and checks, per dialogue line, that the audio
# being spoken matches what is shown on screen at that timestamp. On a
# mismatch the finding is shaped exactly like a human ``timing_mismatch``
# review payload and fed through :func:`rerun_segment_stage` — the SAME
# correction path, no separate one — then the updated video is re-checked, up
# to ``config.MAX_AUTO_QA_FIX_ATTEMPTS`` corrective rounds. The whole loop is
# invisible to the user; only the outcome (passed, or capped with a Bengali
# note) is recorded in the segment's job-status ``qa`` block. A Gemini
# API/parse failure counts as a failed attempt toward the cap and never
# crashes the segment's pipeline run.
# ---------------------------------------------------------------------------

SEGMENT_QA_PROMPT = (
    "You are an automated QA checker for a dubbed manhwa video segment. "
    "Watch the attached video. For each dialogue line below, check whether "
    "the VOICEOVER AUDIO being spoken at that line's time matches what is "
    "SHOWN ON SCREEN at that moment (the scene/action and any on-screen "
    "subtitle text). A line FAILS when the audio does not match the on-screen "
    "scene/action at its timestamp (a voice/scene timing mismatch).\n\n"
    "Dialogue lines (serial, [start_sec - end_sec], text):\n"
    "{lines}\n\n"
    "Respond with ONLY JSON, no commentary, in this exact structure: "
    '{"lines": [{"serial": 1, "pass": true}, {"serial": 2, "pass": false, '
    '"reason": "audio says X but the on-screen scene shows Y"}]}. '
    "Report every line; a line with no issue must have \"pass\": true."
)


def _qa_check_video(key, prompt, video_path):
    """Send one finished segment video to Gemini and parse the per-line QA.

    Mirrors ``subtitle_extract._call_gemini`` exactly (client setup, upload
    reuse, content-block detection); only the response parse differs. Returns
    the raw ``lines`` list. Raises on Gemini/parse failure so the shared
    rotation wrapper can classify and rotate keys.
    """
    client = genai.Client(api_key=key)
    uploaded = subtitle_extract._get_or_upload(client, key, video_path)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[
            genai_types.Part.from_uri(
                file_uri=uploaded.uri, mime_type=uploaded.mime_type
            ),
            prompt,
        ],
    )
    blocked = subtitle_extract._is_content_blocked(None, response)
    if blocked is not None:
        raise subtitle_extract.ContentBlockedError(
            blocked["reason"], blocked["message"]
        )
    data = subtitle_extract._extract_json(response.text)
    raw = data.get("lines", []) if isinstance(data, dict) else []
    if not isinstance(raw, list):
        raise ValueError("malformed QA payload from Gemini")
    return raw


def _qa_dialogue_lines(job_id, seg_dir, upload_root):
    """The segment's dialogue list for the QA check (serial, time, text).

    Text comes from the translated subtitles (``lang_files.entry_text``);
    playback times come from the D4 unified timestamps — what actually plays
    in the final video — falling back to the subtitle timing when unification
    has not run. Reloaded per check round so a D4 re-run's fresh timestamps
    are picked up.
    """
    lang = lang_files.target_lang(job_id, upload_root)
    sub_path = seg_dir / lang_files.subtitles_json(lang)
    if not sub_path.exists():
        return []
    try:
        entries = json.loads(sub_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    aligned = {}
    ts_path = seg_dir / lang_files.timestamps_final(lang)
    if ts_path.exists():
        try:
            ts_list = json.loads(ts_path.read_text(encoding="utf-8"))
            if isinstance(ts_list, list):
                aligned = {
                    int(e["serial"]): e
                    for e in ts_list
                    if isinstance(e, dict) and e.get("serial") is not None
                }
        except (OSError, ValueError, TypeError):
            aligned = {}
    lines = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("serial") is None:
            continue
        try:
            serial = int(entry["serial"])
        except (TypeError, ValueError):
            continue
        timed = aligned.get(serial) or entry
        try:
            start = float(timed.get("start_sec", 0.0))
            end = float(timed.get("end_sec", 0.0))
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        lines.append(
            {
                "serial": serial,
                "text": lang_files.entry_text(entry) or "",
                "start_sec": start,
                "end_sec": end,
            }
        )
    return lines


def build_auto_qa_prompt(lines):
    """Assemble the Gemini QA prompt for a segment's dialogue lines."""
    rendered = "\n".join(
        f'{ln["serial"]}. [{ln["start_sec"]:.3f} - {ln["end_sec"]:.3f}] '
        f'{ln["text"]}'
        for ln in lines
    )
    return SEGMENT_QA_PROMPT.replace("{lines}", rendered)


def _qa_line_failed(line):
    return isinstance(line, dict) and line.get("pass") is False


def _qa_serial(line):
    try:
        return int(line.get("serial"))
    except (TypeError, ValueError):
        return None


def _qa_failure_bn(error):
    """Bengali summary of a QA Gemini failure; never raises."""
    if not isinstance(error, dict):
        return "স্বয়ংক্রিয় QA চেক ব্যর্থ হয়েছে।"
    etype = error.get("type")
    if etype == "call_budget_exceeded":
        return "স্বয়ংক্রিয় QA চেকের জন্য Gemini কল-বাজেট শেষ হয়ে গেছে।"
    if etype == "content_blocked":
        return "স্বয়ংক্রিয় QA চেকটি সামগ্রী নীতিতে আটকে গেছে।"
    message = str(error.get("message") or "").strip()
    if message:
        return f"স্বয়ংক্রিয় QA চেক ব্যর্থ হয়েছে: {message}"
    return "স্বয়ংক্রিয় QA চেক ব্যর্থ হয়েছে।"


def _qa_mismatch_notes(failed):
    """Concise description of the failing lines for the correction re-run."""
    parts = []
    for line in failed:
        serial = _qa_serial(line)
        reason = str(line.get("reason") or "voice/scene mismatch").strip()
        prefix = f"serial {serial}" if serial is not None else "a line"
        parts.append(f"{prefix}: {reason}")
    return "Auto QA detected voice/scene timing mismatch: " + "; ".join(parts)


def _finish_qa_capped(job_id, index, root, log):
    """Record the capped outcome + Bengali note and return the result."""
    job_status_store.record_segment_qa(
        job_id, index, job_status_store.SEGMENT_QA_CAPPED,
        note_bn=job_status_store.SEGMENT_QA_CAP_NOTE_BN, upload_root=root,
    )
    log.warning(
        "job %s seg %d: auto QA reached the %d-attempt cap; releasing to "
        "human review with a note",
        job_id, index, config.MAX_AUTO_QA_FIX_ATTEMPTS,
    )
    return {
        "qa_state": job_status_store.SEGMENT_QA_CAPPED,
        "note_bn": job_status_store.SEGMENT_QA_CAP_NOTE_BN,
    }


def run_auto_qa_gate(job_id, segment, seg_dir, final_path, upload_root=None,
                     call_budget=None, logger_=None):
    """Automated Gemini voice/scene pre-review gate for one segment (F14b).

    Called after a segment's final video is rendered but BEFORE it is marked
    ready for human review. Runs up to ``config.MAX_AUTO_QA_FIX_ATTEMPTS``
    corrective rounds:

    - Gemini watches the final video against the segment's dialogue list.
    - All lines match  -> ``qa_passed``; no user-visible change.
    - A mismatch       -> the finding is shaped exactly like a human
      ``timing_mismatch`` review and fed through ``rerun_segment_stage`` (the
      same correction path); the updated video is then re-checked.
    - A Gemini API/parse failure counts as a failed attempt toward the cap
      (never crashes the pipeline run).
    - Cap reached with a mismatch -> ``qa_capped`` + a Bengali note for F14a.

    Only the given segment's state is touched; other segments are unaffected.
    Returns ``{"qa_state": str|None, "note_bn": str|None}`` (``qa_state`` is
    ``None`` when the gate is skipped, e.g. no final video / no dialogue lines
    / no active Gemini keys).
    """
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    log = logger_ or logger
    index = segment["index"]
    final_path = Path(final_path)
    max_attempts = config.MAX_AUTO_QA_FIX_ATTEMPTS

    if not final_path.exists():
        log.warning("job %s seg %d: no final video for QA; gate skipped",
                    job_id, index)
        return {"qa_state": None, "note_bn": None}
    if not _qa_dialogue_lines(job_id, seg_dir, root):
        log.info("job %s seg %d: no dialogue lines; QA gate skipped",
                 job_id, index)
        return {"qa_state": None, "note_bn": None}
    keys = key_store.get_active_keys()
    if not keys:
        log.warning("job %s seg %d: no active Gemini keys; QA gate skipped",
                    job_id, index)
        return {"qa_state": None, "note_bn": None}

    job_status_store.record_segment_qa(
        job_id, index, job_status_store.SEGMENT_QA_CHECKING, upload_root=root
    )
    rotation = 0
    attempt = 0
    fix_attempts = 0
    while True:
        attempt += 1
        lines = _qa_dialogue_lines(job_id, seg_dir, root)
        if not lines:
            job_status_store.record_segment_qa(
                job_id, index, job_status_store.SEGMENT_QA_PASSED,
                attempt=attempt, outcome="pass", fixed=False,
                upload_root=root,
            )
            log.info("job %s seg %d: no dialogue lines to QA on attempt %d; "
                     "releasing", job_id, index, attempt)
            return {"qa_state": job_status_store.SEGMENT_QA_PASSED,
                    "note_bn": None}
        prompt = build_auto_qa_prompt(lines)
        result, rotation, error = subtitle_extract.call_with_rotation(
            keys, rotation, _qa_check_video, prompt, str(final_path),
            call_budget=call_budget, logger_=log,
        )
        if result is None:
            fix_attempts += 1
            bn = _qa_failure_bn(error)
            job_status_store.record_segment_qa(
                job_id, index, f"qa_fixing_attempt_{attempt}",
                attempt=attempt, outcome="failed", error_bn=bn,
                upload_root=root,
            )
            log.warning(
                "job %s seg %d: auto QA check attempt %d failed: %s",
                job_id, index, attempt, error,
            )
            if fix_attempts >= max_attempts:
                return _finish_qa_capped(job_id, index, root, log)
            continue

        failed = [line for line in result if _qa_line_failed(line)]
        if not failed:
            job_status_store.record_segment_qa(
                job_id, index, job_status_store.SEGMENT_QA_PASSED,
                attempt=attempt, outcome="pass", fixed=False,
                upload_root=root,
            )
            log.info("job %s seg %d: auto QA passed on attempt %d",
                     job_id, index, attempt)
            return {"qa_state": job_status_store.SEGMENT_QA_PASSED,
                    "note_bn": None}

        serials = [s for s in (_qa_serial(line) for line in failed)
                   if s is not None]
        if fix_attempts >= max_attempts:
            job_status_store.record_segment_qa(
                job_id, index, f"qa_fixing_attempt_{attempt}",
                attempt=attempt, outcome="mismatch", issues=serials,
                fixed=False, upload_root=root,
            )
            return _finish_qa_capped(job_id, index, root, log)

        notes = _qa_mismatch_notes(failed)
        review = {
            "round": 1,
            "issues": ["timing_mismatch"],
            "notes": notes,
        }
        log.warning(
            "job %s seg %d: auto QA mismatch on attempt %d (lines %s); "
            "running targeted re-run",
            job_id, index, attempt, serials,
        )
        rerun = rerun_segment_stage(
            job_id, index, round_no=None, review=review,
            upload_root=root, call_budget=call_budget, logger_=log,
        )
        fixed = rerun.get("status") == "ok"
        job_status_store.record_segment_qa(
            job_id, index, f"qa_fixing_attempt_{attempt}",
            attempt=attempt, outcome="mismatch", issues=serials, fixed=fixed,
            upload_root=root,
        )
        fix_attempts += 1
        if not fixed:
            log.warning(
                "job %s seg %d: auto QA fix attempt %d failed: %s",
                job_id, index, attempt, rerun.get("error_bn"),
            )
