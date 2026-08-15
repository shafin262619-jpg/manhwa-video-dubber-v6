"""Pipeline configuration constants.

Central place for all tuning constants. Future chunks must import from here
instead of duplicating values.
"""

# Allowed playback speed ratio range when matching scenes to voiceover length.
SPEED_RATIO_MIN = 0.5
SPEED_RATIO_MAX = 2.0

# Placeholder voice for Hindi TTS. Tune later.
TTS_VOICE_HINDI = "Aoede"

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

# Local Whisper model used as the alignment fallback when Gemini fails (D3).
WHISPER_MODEL = "base"

# Minimum difflib similarity ratio for a Whisper text match to be accepted.
WHISPER_MATCH_MIN_RATIO = 0.55

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

# The draft video duration is validated against the voiceover duration within
# this many frames of the source frame rate (E2).
RENDER_TOLERANCE_FRAMES = 3

# Timeout (seconds) for each ffmpeg render step: clip / concat / mux (E2).
RENDER_TIMEOUT_SEC = 600

# Per-job cap on real Gemini API calls (extraction + translation + auto TTS
# share one CallBudget for the whole job run, U2b). None = unlimited, which is
# also the default when no call_budget is passed (existing callers unchanged).
# Set to an int to give a job a hard quota so a runaway rotation can never burn
# the whole key allowance.
MAX_API_CALLS_PER_JOB = None

# Consecutive serialized subtitle entries whose gap exceeds this (seconds)
# are flagged as possible missing content (QA diagnostics, A1).
SUBTITLE_GAP_FLAG_THRESHOLD_SEC = 6.0

# 3+ consecutive serialized subtitle entries sharing the same start_sec (or
# zero-duration) are flagged as a degenerate extraction cluster (QA
# diagnostics, A2).
SUBTITLE_DUP_CLUSTER_MIN_COUNT = 3

# Max number of targeted re-extraction (Gemini) calls a single job's repair
# pass may make, largest-flagged-range-first (QA repair, B2). Protects the
# shared per-job CallBudget from a runaway repair loop.
SUBTITLE_MAX_REPAIR_ATTEMPTS = 3
