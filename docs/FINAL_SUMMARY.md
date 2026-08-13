# Manhwa Video Dubber — Final Summary (S1 → G2)

This is the end-of-project summary. All development chunks (S1 through G2) are
complete and the full automated regression suite passes. What remains is a
**real-media QA run that only the user (and their friend) can perform** — a
sandboxed AI agent cannot do it because it needs a real Gemini key, real
long-form videos, human eyes/ears, and creative/voice decisions. The checklist
is at the bottom of this document.

- Repo: `https://github.com/shafin262619-jpg/manhwa-video-dubber.git` (branch
  `main`, tag `manhwa-video-dubber-final`)
- Full prompt: `docs/MANHWA_VIDEO_DUBBER_PLAN.md`
- Per-chunk details: `docs/CHANGELOG.md`
- Previous handoff: `docs/HANDOFF_NEXT.md`

---

## 1. What the system does

Takes a Chinese-subtitled manhwa "explain" video, extracts the subtitles,
translates them to Hindi, produces a Hindi voiceover (either auto-TTS or a
user-uploaded track), speed-matches each scene to its voiceover line, lets the
user review/trim per-clip, and renders a final Hindi-dubbed MP4.

## 2. Stage-by-stage summary

| Chunk | Name | Status | Output artifact |
|---|---|---|---|
| S1 | Scaffolding (FastAPI app + package layout + constants) | DONE | `app.py`, `pipeline/`, `pipeline/config.py` |
| A1 | Gemini key settings UI | DONE | `gemini_keys_store.json` (git-ignored) |
| A2 | Video upload + System Start | DONE | `uploads/<job_id>/source.mp4`, `job_meta.json` |
| B1 | Subtitle extraction (Gemini video-understanding, chunked) | DONE | `uploads/<job_id>/subtitles_zh_raw.json` |
| B2 | Subtitle serialization + no-overlap clamp | DONE | `uploads/<job_id>/subtitles_zh.json` |
| C1 | Chinese→Hindi translation | DONE | `subtitles_hi.json`, `.srt`, `subtitles_hi_plain.txt` |
| D1 | Voiceover source choice (auto-TTS vs user-upload) | DONE | `voice_source_choice.json` |
| D2 | Auto Hindi TTS voiceover (Gemini TTS) | DONE | `voiceover_hi.wav`, `timestamps_hi_auto.json` |
| D3 | User-uploaded voiceover + alignment (Gemini/Whisper fallback) | DONE | `voiceover_hi.wav`, `timestamps_hi_upload.json` |
| D4 | Unify auto/upload timestamps + overlap validation | DONE | `timestamps_hi_final.json` |
| E1 | Speed-ratio edit guideline (soft-clamp + flag) | DONE | `edit_guideline.json` |
| E2 | Draft final video (cut + `setpts` + concat + mux) | DONE | `draft_final_video.mp4` |
| F1 | Per-clip review UI (flagged-serial highlight) | DONE | review page + on-the-fly clips |
| F2 | Apply per-clip review edits (partial re-render) | DONE | updated `edit_guideline.json` + re-spliced draft |
| F3 | Final render + download | DONE | `outputs/<job_id>/final_video.mp4` |
| G1 | HTTP-level end-to-end regression (permanent test) | DONE | `pipeline/tests/test_app_orchestration.py` |
| G2 | Final wrap-up + this summary | DONE | `docs/FINAL_SUMMARY.md` |

## 3. How data moves between stages (the data-flow chain)

All intermediate artifacts live under `uploads/<job_id>/` (git-ignored).
Each stage reads the previous stage's file and writes the next one; the G1
regression proves this chain works over HTTP end to end.

```
User uploads video (A2)
  -> uploads/<job_id>/source.mp4  (+ job_meta.json: duration/resolution)

B1  source.mp4                     -> subtitles_zh_raw.json
    (short videos: one Gemini call; >10 min: ffmpeg split into overlapping
     segments, per-segment extraction, timestamps rebased + de-duplicated)

B2  subtitles_zh_raw.json          -> subtitles_zh.json
    (serials 1..N assigned, consecutive ranges clamped to never overlap,
     extraction_failed parts kept as flagged placeholders)

C1  subtitles_zh.json              -> subtitles_hi.json + .srt + _plain.txt
    (serial count/order preserved; per-line translation_fallback flag)

D1  (user chooses voice source)    -> voice_source_choice.json
     mode = auto_tts | user_upload

D2  subtitles_hi.json (auto path)  -> voiceover_hi.wav + timestamps_hi_auto.json
    (one Gemini TTS call per line, ffprobe-measured durations, cumulative
     timestamps; tts_failed -> silence placeholder + flag)

D3  subtitles_hi.json (upload path)-> voiceover_hi.wav + timestamps_hi_upload.json
    (user's mp3/wav/m4a normalized to voiceover_hi.wav; Gemini audio
     alignment -> Whisper fallback -> equal-split fallback; alignment_fallback
     + alignment_source flags per serial)

D4  voice_source_choice.json       -> timestamps_hi_final.json
    (picks D2 or D3 file; maps tts_failed / alignment_fallback onto
     flagged + flag_reason; deterministic overlap clamp; verifies shared
     voiceover_hi.wav exists — later stages are mode-agnostic)

E1  subtitles_zh.json  +           -> edit_guideline.json
    timestamps_hi_final.json
    (pts_multiplier = target_duration / source_duration per serial;
     outside 0.5..2.0 -> flagged extreme_speed_ratio; zero/negative duration
     -> pts_multiplier 1.0 + invalid_duration; pure Python, no ffmpeg/network)

E2  edit_guideline.json            -> draft_final_video.mp4
    + source.mp4 + voiceover_hi.wav
    (per serial: cut [source_start, source_end] video-only, setpts stretch,
     concat in serial order, mux voiceover as audio; ffprobe-validated against
     voiceover duration within a few frames)

F1  draft_final_video.mp4 +        -> review page per serial
    edit_guideline.json + subtitles_hi.json
    (clip player streams each serial's target segment on the fly; trim inputs;
     flagged serials highlighted)

F2  review edits (trim form)       -> updated edit_guideline.json + re-spliced
    draft_final_video.mp4
    (only the edited serial changes; target timing fixed, pts_multiplier
     recomputed, extreme_speed_ratio re-evaluated; partial re-render of just
     that clip + concat/mux, then E2-style validation)

F3  draft_final_video.mp4          -> outputs/<job_id>/final_video.mp4
    (normalize to H.264/AAC + faststart; download served at
     /download/<job_id>)
```

Shared helpers (no duplication): `subtitle_extract.call_with_rotation`
multi-key round-robin is reused by C1/D2; key masking everywhere so raw keys
never leak in API responses.

## 4. Automated coverage (what is already proven)

- `python3 -m unittest discover -s pipeline/tests` — **184 tests OK**
  (module-level tests per stage + `TestClient` HTTP endpoint tests).
- `pipeline/tests/test_app_orchestration.py` (G1, permanent) drives the app
  purely through HTTP endpoints over the full chain: add key → upload
  (B1+B2+C1) → choose auto-TTS (D2+D4+E1+E2) → review loads per-clip data →
  review edit (F2 partial re-render) → final render (F3) → download serves the
  final file. Gemini mocked, auto-cut/render ffmpeg mocked, D2 clips are real
  ffmpeg silence (deterministic, no network).
- Conventions enforced throughout: no uncaught exceptions on Gemini/ffmpeg
  failures (fall back + flag), constants from `pipeline/config.py`, secrets
  never committed.

---

## 5. These steps a sandboxed AI agent CANNOT do — the user must do them

The automated suite proves the *machinery* works with mocked Gemini and
synthetic media. It cannot prove the *output quality* of the real pipeline.
The following require a real Gemini key, real videos, and human eyes/ears.
They are intentionally not part of any sandboxed-agent run.

### (a) Full end-to-end run on a real video with a real Gemini key

Use one of your friend's real manhwa "explain" videos and a real Gemini API
key (add it under `/settings`). Verify each of these with eyes/ears:

1. **Subtitle extraction accuracy** (B1) — do the extracted Chinese subtitles
   match what is actually on screen? Any missing/merged/mis-timed lines?
   Download the SRT at `/download/{job_id}/subtitles?format=srt` and spot-check
   against the source video.
2. **Translation naturalness** (C1) — is the Hindi natural and correct for a
   manhwa explain video? Any `translation_fallback` lines (kept as Chinese
   because Gemini failed) that need manual fixing?
3. **Voiceover quality** (D2/D3) — listen to `voiceover_hi.wav`. If auto-TTS:
   does the Hindi voice sound good? Any `tts_failed` silences? If
   user-upload: is the recorded audio clean, and is the alignment accurate?
   (Check `timestamps_hi_auto.json` / `timestamps_hi_upload.json`.)
4. **Speed-adjusted scenes** (E2) — the biggest thing an agent cannot judge:
   do the stretched/squeezed scenes look natural? Pay special attention to the
   serials flagged `flagged: true, flag_reason: "extreme_speed_ratio"` on the
   `/review/{job_id}` page — these are outside the 0.5×–2.0× safe band and are
   the most likely to look wrong. Use the review page's trim controls to fix
   any that look bad, then re-render.
5. **Final lip-sync / scene-sync** (F3) — in `outputs/<job_id>/final_video.mp4`
   (or `/download/{job_id}`), does the Hindi speech actually line up with the
   scenes in a watchable way? Watch a couple of clips with sound on.

### (b) Long-video (>10 min) chunking test (B1)

B1's `LONG_VIDEO_CHUNK_THRESHOLD_SEC` (600 s) path — ffmpeg split into
overlapping segments, per-segment Gemini extraction, timestamp rebasing and
dedup — is unit-tested but has not run against a real long video. Upload a
video longer than 10 minutes and confirm:

- subtitles across the whole video are extracted continuously (no gap or jump
  at a segment boundary),
- no duplicated subtitle lines appear around the segment seams.

### (c) TTS voice / persona selection (D2) — creative decision

`TTS_VOICE_HINDI = "Aoede"` in `pipeline/config.py` is a placeholder. Picking
the actual Hindi voice/persona for the explain-video style is a creative
decision you must make by listening to real output — an agent cannot decide
the tone/branding of the voice for you. Change it in `config.py` and re-run
D2 to try candidates.

### (d) BGM preservation — confirm with your friend, then request separately

The original Chinese explain video's background music is **not** preserved:
E2 deliberately discards the source audio (`-an` per clip) and muxes only the
Hindi voiceover, so the final video has no BGM track. This was a deliberate
scope decision (see the Assumptions note in the plan). **Before treating the
project as done, confirm with your friend whether BGM preservation is wanted.**
If yes, ask for it as a separate follow-up feature plan (e.g. duck the original
BGM under the voiceover, or keep a low-volume music bed) — it is not part of
this plan's scope and should not be silently bolted on.

### (e) Run the pre-flight dry-run check before/after every real-media run

`pipeline/dry_run_check.py` এখন আছে — offline JSON-সংগতি যাচাই (কোনো
network/ffmpeg/Gemini কল ছাড়া, সেকেন্ডেই সেরে যায়)। প্রতিটা real-media
রান-এর **আগে ও পরে** চালিয়ে দেখুন (খরচবহুল TTS/রেন্ডার শুরু করার আগে বড়
ভুল ধরার জন্য):

```bash
python3 -m pipeline.dry_run_check --job-id <job_id>
```

Exit 0 = কোনো ব্লকিং error নেই; 1 = serial mismatch/gap/duplicate বা
invalid/overlapping duration — রেন্ডারের আগে ফিক্স করা আবশ্যক।

---

## 6. How to run

```bash
# install deps (ffmpeg must be on PATH)
pip install -r requirements.txt

# start the web app (local preview)
uvicorn app:app --host 0.0.0.0 --port 5000
```

1. Open the app, add a real Gemini key under **Settings**.
2. **System Start**: upload the Chinese-subtitled video (B1+B2+C1 run
   automatically).
3. **/voiceover/{job_id}/choose**: pick auto-TTS or upload your own audio
   (D2/D3 → D4).
4. **/review/{job_id}**: inspect every serial, fix flagged clips via the trim
   form (F2).
5. **Final Render**: `/final/{job_id}` → normalize + download
   `final_video.mp4` (F3).

## 7. Known follow-ups (not in this plan's scope)

- BGM preservation (background music bed / ducking) — pending your friend's
  confirmation, request as a separate feature plan.
- Final Hindi TTS voice/persona selection (creative decision — see 5(c)).
- Production concerns if this ever leaves local use: job queues/background
  tasks for long videos, storage cleanup policy, concurrency — none were in
  scope.
