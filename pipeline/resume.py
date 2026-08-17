"""Resume-from-interruption (F9).

A job killed mid-chain (process crash, manual stop, transient failure) can be
resumed from its first missing artifact instead of re-running everything. The
resume point is derived purely from files already on disk — each chain stage
writes a distinctive artifact, so the *presence* of that artifact is the
proof the stage finished:

===============  =================================================
stage            artifact (relative to the job dir)
===============  =================================================
F1_extract       ``subtitles_zh_raw.json``
C1_translate     ``subtitles_hi.json``
D2_voiceover     ``timestamps_hi_auto.json``
D3_align         ``timestamps_hi_upload.json``
D4_unify         ``timestamps_hi_final.json``
E1_guideline     ``edit_guideline.json``
E2_draft         ``draft_final_video.mp4``
F3_final         ``outputs/<job_id>/final_video.mp4``
===============  =================================================

``find_resume_point`` returns the first missing stage. A job that never
finished the upload pipeline (no ``subtitles_hi.json``) reports the sentinel
``"upload_pipeline"`` — there is no D2+ chain to resume until extraction +
translation exist. ``resume_job`` then re-runs the right chain (auto-TTS vs
user-upload, from the voice-source choice) with ``start_from=<resume point>``
so the completed stages are skipped, never re-run.

F12c (Part A): the upload pipeline is itself resumable at sub-stage
granularity. ``find_upload_resume_point`` derives which upload sub-stage
(F1 extraction / import+gap-fill, B2 build/repair, whisper cross-check, C1
translate) is first missing from its artifact, using the same presence-proof
pattern as the D2+ chain. The app wires that point back into
``_run_upload_pipeline`` so an interrupted upload resumes without re-running
completed sub-stages.
"""

from pathlib import Path

from pipeline import full_auto_chain, lang_files, render_final, video_ingest, voiceover_unify

# Upload-pipeline sub-stages and the artifact that proves each one finished.
# Order matters: ``find_upload_resume_point`` returns the first missing one.
# - F1        ``subtitles_zh_raw.json``   (Gemini extraction or transcript import + gap-fill)
# - B2        ``subtitles_zh.json``       (build/repair + subtitle_qa.json)
# - whisper   ``subtitle_qa_whisper.json``(local Whisper cross-check; always written, best-effort)
# - C1        ``subtitles_hi.json``       (translation)
UPLOAD_PIPELINE_STAGES = (
    ("upload_F1", "subtitles_zh_raw.json"),
    ("upload_B2", "subtitles_zh.json"),
    ("upload_whisper", "subtitle_qa_whisper.json"),
    ("upload_C1", "subtitles_hi.json"),
)


def _stage_artifacts(job_id, upload_root, mode):
    """Ordered (stage_name, artifact_path_or_callable) checks for a mode."""
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = root / job_id
    lang = lang_files.target_lang(job_id, upload_root)
    artifacts = [
        ("D2_voiceover", job_dir / lang_files.timestamps_auto(lang))
        if mode == "auto_tts"
        else ("D3_align", job_dir / lang_files.timestamps_upload(lang)),
        ("D4_unify", job_dir / lang_files.timestamps_final(lang)),
        ("E1_guideline", job_dir / "edit_guideline.json"),
        ("E2_draft", job_dir / "draft_final_video.mp4"),
        ("F3_final", lambda: render_final.final_video_path(job_id).exists()),
    ]
    return artifacts


def find_resume_point(job_id, upload_root=None):
    """Return the first stage whose artifact is missing, or None when complete.

    ``"upload_pipeline"`` is returned when the job has no translated-subtitle
    file yet (extraction/translation unfinished) — the D2+ chains have nothing
    to resume until that exists. Never raises for a missing job dir: a job with
    no translated-subtitle file reports ``"upload_pipeline"``.
    """
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = root / job_id
    if not (job_dir / lang_files.subtitles_json(lang_files.target_lang(job_id, upload_root))).exists():
        return "upload_pipeline"

    mode = voiceover_unify.get_voice_source(job_id, upload_root) or "auto_tts"
    for stage, artifact in _stage_artifacts(job_id, upload_root, mode):
        present = artifact() if callable(artifact) else artifact.exists()
        if not present:
            return stage
    return None


def find_upload_resume_point(job_id, upload_root=None):
    """Return the first incomplete upload-pipeline sub-stage, or None.

    Uses the same artifact-presence proof as :func:`find_resume_point` but at
    sub-stage granularity within the upload pipeline:

    - ``None`` — the upload pipeline is fully done (``subtitles_hi.json``
      exists); the D2+ chain resume (``find_resume_point``) governs from here.
    - ``"upload_pipeline"`` — nothing of the upload pipeline has started yet
      (no ``subtitles_zh_raw.json``); there is nothing to resume, the job
      needs a fresh start.
    - ``"upload_F1"`` / ``"upload_B2"`` / ``"upload_whisper"`` /
      ``"upload_C1"`` — the first sub-stage whose artifact is missing; resume
      ``_run_upload_pipeline`` from here, skipping earlier completed stages.

    Never raises for a missing/malformed job dir — missing files simply report
    the next stage to run.
    """
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = root / job_id
    if (job_dir / lang_files.subtitles_json(
        lang_files.target_lang(job_id, upload_root)
    )).exists():
        return None
    if not (job_dir / "subtitles_zh_raw.json").exists():
        return "upload_pipeline"
    for stage, name in UPLOAD_PIPELINE_STAGES:
        if not (job_dir / name).exists():
            return stage
    return None


def resume_job(job_id, upload_root=None):
    """Continue an interrupted job from its first missing artifact onward.

    Returns the chain result dict (see ``run_auto_tts_chain`` /
    ``run_user_upload_chain``). Raises ``RuntimeError`` when the job is
    already complete or has not finished the upload pipeline; stage failures
    propagate exactly like a normal chain run.
    """
    point = find_resume_point(job_id, upload_root)
    if point is None:
        raise RuntimeError(f"job {job_id} is already complete — nothing to resume")
    if point == "upload_pipeline":
        raise RuntimeError(
            f"job {job_id} has not finished subtitle extraction/translation yet; "
            "resume the upload pipeline before resuming the dubbing chain"
        )

    mode = voiceover_unify.get_voice_source(job_id, upload_root) or "auto_tts"
    if mode == "auto_tts":
        return full_auto_chain.run_auto_tts_chain(job_id, start_from=point)
    return full_auto_chain.run_user_upload_chain(job_id, start_from=point)
