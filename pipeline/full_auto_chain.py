"""Full-auto pipeline orchestration wrappers (FA-B1 / FA-B2 / F9).

Group B provides two pure-Python functions that run the whole backend chain
behind a single voice-source choice down to the **final** video (F3 included):

- ``run_auto_tts_chain`` — D2 (auto TTS) -> D4 (unify) -> E1 (edit guideline)
  -> E2 (draft render) -> F3 (final render). Used by the ``auto_tts`` path.
- ``run_user_upload_chain`` — D3 (align uploaded audio) -> D4 -> E1 -> E2 ->
  F3. Precondition: ``voiceover_hi.wav`` is already saved on disk (via
  ``voiceover_upload.save_uploaded_voiceover``); this function does not save it.

Both functions are deliberately *standalone*: they are not wired into any HTTP
route yet (that happens in groups C/D). They raise on failure — the exception
propagates to the caller (the existing ``_friendly_error`` / job_status
pattern in ``app.py`` catches it), never a silent swallow, so a mid-chain
failure stops the following steps instead of leaving a partial state.

F9: every stage is wrapped with ``job_status.run_stage`` so the status file
gets a ``running`` entry for each stage in order, then ``done``. Both chains
also accept ``start_from`` — the name of the first stage to run — so a resumed
job skips the stages whose artifacts already exist on disk (see
``pipeline/resume.py``); the skipped stages are never re-run and their result
keys are reported as ``None`` in the returned dict.
"""

from pipeline import (
    auto_cut,
    edit_guideline,
    job_status as job_status_store,
    render_final,
    voiceover_auto,
    voiceover_unify,
    voiceover_upload,
)

# Canonical chain stage order. Resume skips everything before ``start_from``.
AUTO_CHAIN_STAGES = (
    "D2_voiceover",
    "D4_unify",
    "E1_guideline",
    "E2_draft",
    "F3_final",
)

USER_CHAIN_STAGES = (
    "D3_align",
    "D4_unify",
    "E1_guideline",
    "E2_draft",
    "F3_final",
)

CHAIN_STAGE_ORDER = (
    "D2_voiceover",
    "D3_align",
    "D4_unify",
    "E1_guideline",
    "E2_draft",
    "F3_final",
)


def _stage_progress(job_id, stage):
    """Return a ``progress_cb(processed, total)`` writing stage status."""

    def cb(processed, total):
        job_status_store.write_status(
            job_id, stage, "running",
            extra={"progress": {"processed": processed, "total": total}},
        )

    return cb


def _run_chain(job_id, steps, start_from=None):
    """Run ``steps`` (stage-name -> 0-arg callable) with status wrapping.

    When ``start_from`` is set, every step before it is skipped (its artifact
    is assumed present on disk) and only that stage onward runs. ``start_from``
    must name one of ``steps`` — a resume point that lies outside this chain
    (e.g. the upload pipeline) is the caller's job to reject first. Returns
    ``{stage_name: result_or_None}`` for the chain stages, where skipped
    stages are ``None``.
    """
    if start_from is not None and start_from not in dict(steps):
        raise ValueError(f"start_from {start_from!r} is not a stage of this chain")
    started = start_from is None
    results = {}
    for stage, callable_ in steps:
        if not started:
            if stage != start_from:
                results[stage] = None
                continue
            started = True
        results[stage] = job_status_store.run_stage(job_id, stage, callable_)
    return results


def run_auto_tts_chain(job_id, call_budget=None, start_from=None):
    """D2 (auto TTS) -> D4 (unify) -> E1 (edit guideline) -> E2 (draft
    render) -> F3 (final render).

    ``start_from`` (optional) names the first stage to run; earlier stages are
    skipped and reported ``None`` in the result (resume, F9).

    Raises on failure and lets the exception propagate to the caller (no
    uncaught exceptions, no silent swallow): a mid-chain failure stops the
    following steps immediately.

    Returns ``{"voiceover": <D2 result>, "draft": <E2 result>, "final": <F3
    result>}`` so the caller (group C) can persist all of them in the job
    status (the draft result carries the non-blocking duration warning).
    """

    def _d2():
        return voiceover_auto.generate_auto_voiceover(
            job_id, call_budget=call_budget
        )

    steps = [
        ("D2_voiceover", _d2),
        ("D4_unify", lambda: voiceover_unify.unify_voiceover_timestamps(job_id)),
        ("E1_guideline", lambda: edit_guideline.build_edit_guideline(job_id)),
        (
            "E2_draft",
            lambda: auto_cut.build_draft_video(
                job_id, progress_cb=_stage_progress(job_id, "E2_draft")
            ),
        ),
        ("F3_final", lambda: render_final.finalize_video(job_id)),
    ]
    results = _run_chain(job_id, steps, start_from=start_from)
    return {
        "voiceover": results.get("D2_voiceover"),
        "draft": results.get("E2_draft"),
        "final": results.get("F3_final"),
    }


def run_user_upload_chain(job_id, start_from=None):
    """D3 (align uploaded audio) -> D4 (unify) -> E1 (edit guideline) -> E2
    (draft render) -> F3 (final render).

    ``start_from`` (optional) names the first stage to run; earlier stages are
    skipped and reported ``None`` in the result (resume, F9).

    Precondition: ``voiceover_hi.wav`` is already saved on disk for the job
    (via ``voiceover_upload.save_uploaded_voiceover``) — this function does
    not save the audio, it only assumes the file exists.

    Raises on failure and lets the exception propagate to the caller (no
    uncaught exceptions, no silent swallow): a mid-chain failure stops the
    following steps immediately.

    Returns ``{"alignment": <D3 result>, "draft": <E2 result>, "final": <F3
    result>}`` so the caller (group D) can persist all of them in the job
    status (the alignment + draft results carry the non-blocking warnings).
    """
    steps = [
        ("D3_align", lambda: voiceover_upload.align_uploaded_voiceover(job_id)),
        ("D4_unify", lambda: voiceover_unify.unify_voiceover_timestamps(job_id)),
        ("E1_guideline", lambda: edit_guideline.build_edit_guideline(job_id)),
        (
            "E2_draft",
            lambda: auto_cut.build_draft_video(
                job_id, progress_cb=_stage_progress(job_id, "E2_draft")
            ),
        ),
        ("F3_final", lambda: render_final.finalize_video(job_id)),
    ]
    results = _run_chain(job_id, steps, start_from=start_from)
    return {
        "alignment": results.get("D3_align"),
        "draft": results.get("E2_draft"),
        "final": results.get("F3_final"),
    }
