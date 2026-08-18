"""Pipeline configuration constants.

Central place for all tuning constants. Future chunks must import from here
instead of duplicating values.
"""

# Allowed playback speed ratio range when matching scenes to voiceover length.
SPEED_RATIO_MIN = 0.5
SPEED_RATIO_MAX = 2.0

# Placeholder voice for Hindi TTS. Tune later.
#
# Gemini TTS voices are stylistic descriptors, NOT language-specific: the
# language of the speech comes from the input text, and the docs' supported
# languages table includes Bangla (bn), Hindi (hi) and English (en) with the
# same 30 voice_name options. ``TTS_VOICES`` maps each supported target_lang
# to a voice; hi keeps the long-standing placeholder so its output is
# byte-identical to pre-F12f, bn/en get distinct placeholders (tune later).
TTS_VOICE_HINDI = "Aoede"
TTS_VOICE_BN = "Zephyr"
TTS_VOICE_EN = "Puck"

# Single source of truth for the TTS voice per target_lang (F12f).
# Unknown/newer language codes fall back to the Hindi voice.
TTS_VOICES = {
    "hi": TTS_VOICE_HINDI,
    "bn": TTS_VOICE_BN,
    "en": TTS_VOICE_EN,
}

# Model used for audio-generating TTS (Gemini TTS path).
#
# DELIBERATE PIN (chunk U0, corrected 2026-08-13): pinned to match
# BlueprintTube's pipeline/voiceover.py MODEL_NAME exactly, per the original
# robustness plan. This is a field-verified choice, not a guess:
# - gemini-2.5-flash-preview-tts has a real, workable free tier (confirmed by
#   BlueprintTube's own production use).
# - gemini-3.1-flash-tts-preview (the model this was briefly switched to in
#   FIX1, reasoning from Google's deprecation-page "recommended replacement"
#   note) turned out to have a much smaller free-tier daily quota per key
#   (observed: HTTP 429 RESOURCE_EXHAUSTED, limit 10 requests/day, across
#   every rotated key) — a "recommended replacement" on the deprecation page
#   is not a promise of equivalent free-tier quota, and that gap is what
#   caused every Gemini TTS call to fail in production.
# - Do NOT switch to a pro/paid TTS variant (e.g. gemini-2.5-pro-preview-tts)
#   either: it has been paid-only with no free quota since 2026-04-01, so a
#   free API key fails with HTTP 429 on the first request (BlueprintTube
#   parity, confirmed).
# Only change this again with a billed account in place and as an explicit,
# documented decision — not because a deprecation page suggests a newer name.
TTS_MODEL = "gemini-2.5-flash-preview-tts"

# Sample rate used when normalizing TTS clips and silence placeholders.
TTS_SAMPLE_RATE = 24000

# Silence placeholder duration (seconds) used when TTS fails for a serial.
TTS_FAIL_SILENCE_SEC = 2.0

# Videos longer than this (seconds) are chunked as B1 before processing.
#
# DELIBERATE (chunk C1, corrected after a real-world failure): originally
# 600s. A real ~5-6 minute dialogue-dense manhwa-dub video came in UNDER
# that threshold and was sent to Gemini in a single call — the model
# dropped a ~50-second/37-line dialogue-heavy block entirely and mis-timed
# several others into duplicate-timestamp clusters. Lowering this to 90s
# means even short videos get segmented into smaller, easier-for-Gemini
# chunks (with SUBTITLE_OVERLAP_SEC overlap + dedup, same as before), which
# both improves per-segment timestamp accuracy and reduces missed dialogue.
# The trade-off is more Gemini calls per job (still capped by
# MAX_API_CALLS_PER_JOB) and more ffmpeg segment-cutting time.
LONG_VIDEO_CHUNK_THRESHOLD_SEC = 90

# Overlap (seconds) between consecutive B1 video segments when chunking.
SUBTITLE_OVERLAP_SEC = 30.0

# Subtitles with the same normalized text whose starts are within this many
# seconds are treated as overlap duplicates and de-duplicated.
SUBTITLE_DEDUP_TOLERANCE_SEC = 10.0

# F13b: target length (seconds) of each per-segment pipeline run. Long videos
# are split at natural transcript gaps (never mid-dialogue) into segments of
# roughly this duration, and the whole downstream chain (B2 -> C1 -> D2-F3)
# runs independently on one segment at a time. A video with no usable gaps or
# shorter than the target yields exactly one segment (the existing flow).
SEGMENT_TARGET_DURATION_SEC = 300

# F13b: a trailing piece of video shorter than this fraction of the target
# duration is folded into the previous segment instead of becoming a tiny
# final segment.
SEGMENT_MIN_TRAILING_RATIO = 0.5

# Gemini model used for video-understanding subtitle extraction.
# gemini-2.0-flash shut down on 2026-06-01; Google's recommended replacement is
# gemini-3.6-flash (no shutdown date announced).
# TODO: re-check https://ai.google.dev/gemini-api/docs/deprecations periodically.
GEMINI_MODEL = "gemini-3.6-flash"

# Gemini model used for audio-understanding voiceover alignment (D3).
# Same deprecated gemini-2.0-flash lineage, same replacement as GEMINI_MODEL.
# TODO: re-check https://ai.google.dev/gemini-api/docs/deprecations periodically.
ALIGNMENT_MODEL = "gemini-3.6-flash"

# Backoff delays (seconds) used when retrying a rate-limited / transient Gemini
# call on the SAME key before that key is rotated out. E.g. 10s -> 30s -> 60s.
# NOTE (U2b): no longer used by call_with_rotation — the v2 rotation
# (gemini_rotation.call_with_rotation_v2) classifies errors and rotates
# immediately, with no same-key backoff. Kept for reference/history.
GEMINI_RETRY_BACKOFF_SEC = (10, 30, 60)

# HTTP statuses treated as transient by the Gemini retry logic: they are retried
# with the GEMINI_RETRY_BACKOFF_SEC delays on the same key before rotating.
# NOTE (U2b): superseded by gemini_rotation.classify_error's marker rules.
GEMINI_TRANSIENT_HTTP_CODES = (408, 429, 500, 502, 503, 504)

# Local Whisper model used as the timing authority for Chinese subtitle
# extraction (F1-F3) and user-uploaded Hindi voiceover alignment (D3), and as
# the alignment fallback when Gemini fails (D3).
#
# DELIBERATE (chunk F8): upgraded from "base" to "small". Whisper is now the
# primary timing authority (F8), so segment/word boundaries must be reliable;
# "base" gives coarse, often merged boundaries. "small" is a reasonable
# accuracy/runtime trade-off on the pipeline machines.
WHISPER_MODEL = "small"

# Optional per-language Whisper model overrides. None = fall back to
# WHISPER_MODEL. The language itself is passed explicitly to Whisper where the
# target language is known (D3 always transcribes the Hindi voiceover with
# language="hi"); ZH extraction relies on Whisper's auto language detection.
WHISPER_MODEL_ZH = None
WHISPER_MODEL_HI = None

# Minimum difflib similarity ratio for a Whisper text match to be accepted.
WHISPER_MATCH_MIN_RATIO = 0.55

# F1-F3: a Gemini subtitle line is merged onto a Whisper segment (using the
# segment's timing and Gemini's text) only when at least this fraction of the
# line's span overlaps the segment AND their texts are similar enough
# (SequenceMatcher ratio >= 0.3, see subtitle_extract._whisper_merge_subtitles).
SUBTITLE_OVERLAP_MATCH_MIN = 0.5

# D3: a Gemini-resolved (secondary-pass) serial is accepted only when its
# reported end_sec is within this many seconds of the last Whisper-detected
# speech end. Gemini can never place audio past what Whisper actually heard.
WHISPER_TAIL_TOLERANCE_SEC = 1.0

# Deterministic output for structured extraction.
GEMINI_TEMPERATURE = 0.0

# Per-serial draft clip re-encode settings (E2).
RENDER_VIDEO_CODEC = "libx264"
RENDER_VIDEO_PRESET = "veryfast"
RENDER_PIX_FMT = "yuv420p"

# Audio codec used when muxing the Hindi voiceover into the draft video (E2).
RENDER_AUDIO_CODEC = "aac"

# Default source frame rate used to size the validation tolerance when the
# source fps cannot be read (E2).
RENDER_DEFAULT_FPS = 25

# The draft video duration is validated against the source video duration
# within this many frames of the source frame rate (E2). This strict check
# applies to the auto-TTS path only (clip durations are measured/precise); the
# user_upload path never blocks on total duration (see below).
RENDER_TOLERANCE_FRAMES = 3

# user_upload draft-duration tolerance (E2, informational only after the
# duration-check removal). A human-recorded voiceover has natural pacing
# variance, and translated dialogue is legitimately a different length from
# the original video, so the draft is never BLOCKED on total-duration for the
# user_upload path. These two values only size the reported tolerance /
# mismatch diagnostics now; they do not gate the render.
USER_UPLOAD_DURATION_TOLERANCE_SEC = 3.0
USER_UPLOAD_DURATION_TOLERANCE_RATIO = 0.05

# Non-blocking "did you upload the right file?" warning threshold for the
# user_upload path: when the rendered draft (which equals the uploaded
# voiceover's total length) is at least this many times LONGER or this fraction
# SHORTER than the source video, the job still completes but the result page
# shows a warning banner. 5x (or 1/5x) is a strong wrong-file signal while
# staying well clear of legitimate translation drift (e.g. the real-media test
# of 523s audio on a 303s video is ~1.7x and produces no warning).
USER_UPLOAD_DURATION_WARNING_RATIO = 5.0

# Timeout (seconds) for each ffmpeg render step: clip / concat / mux (E2).
RENDER_TIMEOUT_SEC = 600

# A source segment shorter than this (seconds) is degenerate: ffmpeg aborts
# with "-to value smaller than -ss" on a zero/negative window, which used to
# crash the whole job after a subtitle zero-duration pile-up (E7). auto_cut
# falls back to cutting a minimal real window and stretching it to the target
# duration so the draft timeline stays intact instead of failing the job.
RENDER_MIN_SEGMENT_DURATION_SEC = 0.05

# Per-job cap on real Gemini API calls (extraction + translation + auto TTS
# share one CallBudget for the whole job run, U2b). None = unlimited, which is
# also the default when no call_budget is passed (existing callers unchanged).
# Set to an int to give a job a hard quota so a runaway rotation can never burn
# the whole key allowance.
MAX_API_CALLS_PER_JOB = None

# Consecutive serialized subtitle entries whose gap exceeds this (seconds)
# are flagged as possible missing content (QA diagnostics, A1).
SUBTITLE_GAP_FLAG_THRESHOLD_SEC = 6.0

# Consecutive serialized subtitle entries sharing the same start_sec are
# flagged as a degenerate extraction cluster only when at least this many
# consecutive entries are involved (QA diagnostics, A2). Zero-duration
# entries (start_sec == end_sec) are ALWAYS flagged, even a single one —
# they are never valid and must reach the repair pass (B2) so they cannot
# leak into the final SRT.
SUBTITLE_DUP_CLUSTER_MIN_COUNT = 3

# Min consecutive raw subtitle entries whose timestamps collide with the
# running end cursor (each start_sec < previous end_sec) to be treated as a
# degenerate collision cluster (E7). Such runs are no longer clamped one-by-one
# to the same timestamp (which collapsed them into a zero-duration pile-up and
# later crashed the ffmpeg cut); they are redistributed with non-zero,
# text-length-weighted durations during serialization instead.
SUBTITLE_COLLISION_CLUSTER_MIN_COUNT = 3

# Per-entry minimum duration (seconds) each entry of a redistributed collision
# cluster is guaranteed (E7). Used both as the weighting floor when the next
# anchor entry leaves enough room, and as the fallback duration when it does
# not (0.8s is comfortably above the ffmpeg min-window guard so the rendered
# clips are never degenerate).
SUBTITLE_MIN_SERIAL_DURATION_SEC = 0.8

# Max number of targeted re-extraction (Gemini) calls a single job's repair
# pass may make, largest-flagged-range-first (QA repair, B2). Protects the
# shared per-job CallBudget from a runaway repair loop.
SUBTITLE_MAX_REPAIR_ATTEMPTS = 3

# whisper_cross_check() flags a mismatch when Gemini-extracted covered
# duration is below this fraction of Whisper's independently-measured
# spoken-audio duration (QA verification, D1).
SUBTITLE_COVERAGE_MISMATCH_RATIO = 0.75

# F12b: a consecutive pair of uploaded-transcript entries (SRT/VTT) whose gap
# (start of the next minus end of the previous) exceeds this many seconds is
# treated as missing content and re-extracted from the video with Gemini.
# Mirrors the QA flag threshold SUBTITLE_GAP_FLAG_THRESHOLD_SEC above.
TRANSCRIPT_GAP_FILL_THRESHOLD_SEC = 6.0

# Cap on the number of gap windows re-extracted per upload job (largest gaps
# first). None = unlimited (default). Guards the shared per-job Gemini
# CallBudget from a transcript with many tiny holes.
TRANSCRIPT_GAP_FILL_MAX_WINDOWS = None

# F14b Part 2: max automated Gemini pre-review QA fix attempts per segment
# before the segment is released to human review anyway (capped). Each Gemini
# voice/scene-sync check round counts toward this cap, whether it found a
# mismatch (and triggered a targeted re-run) or the check itself failed.
MAX_AUTO_QA_FIX_ATTEMPTS = 3
