"""Full-auto pipeline orchestration wrappers (FA-B1 / FA-B2).

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
"""

from pipeline import auto_cut, edit_guideline, render_final, voiceover_auto, voiceover_unify, voiceover_upload


def run_auto_tts_chain(job_id, call_budget=None):
    """D2 (auto TTS) -> D4 (unify) -> E1 (edit guideline) -> E2 (draft
    render) -> F3 (final render).

    Raises on failure and lets the exception propagate to the caller (no
    uncaught exceptions, no silent swallow): a mid-chain failure stops the
    following steps immediately.

    Returns ``{"voiceover": <D2 result>, "draft": <E2 result>, "final": <F3
    result>}`` so the caller (group C) can persist all of them in the job
    status (the draft result carries the non-blocking duration warning).
    """
    voiceover_result = voiceover_auto.generate_auto_voiceover(
        job_id, call_budget=call_budget
    )
    voiceover_unify.unify_voiceover_timestamps(job_id)
    edit_guideline.build_edit_guideline(job_id)
    draft_result = auto_cut.build_draft_video(job_id)
    final_result = render_final.finalize_video(job_id)
    return {"voiceover": voiceover_result, "draft": draft_result, "final": final_result}


def run_user_upload_chain(job_id):
    """D3 (align uploaded audio) -> D4 (unify) -> E1 (edit guideline) -> E2
    (draft render) -> F3 (final render).

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
    alignment_result = voiceover_upload.align_uploaded_voiceover(job_id)
    voiceover_unify.unify_voiceover_timestamps(job_id)
    edit_guideline.build_edit_guideline(job_id)
    draft_result = auto_cut.build_draft_video(job_id)
    final_result = render_final.finalize_video(job_id)
    return {"alignment": alignment_result, "draft": draft_result, "final": final_result}
