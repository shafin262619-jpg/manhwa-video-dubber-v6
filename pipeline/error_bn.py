"""Bengali user-facing error messages (F11).

Mirror of ``app._friendly_error``: the pipeline records a short English
``detail`` on every error status (backward-compat — the review page and tests
read it), and F11 adds ``detail_bn`` next to it so the UI can show the primary
message in Bengali. ``explain_bn`` maps the same exception classes
``_friendly_error`` handles and must never raise: a failure inside the mapper
falls back to a generic Bengali message, never an exception.

Explicit non-goal: F12 owns transcript/format validation. This module only has
a placeholder message for malformed transcript data so the UI has something
sane to show until real validation lands.
"""

import json
import subprocess

from pipeline import gemini_rotation, stages

# Umbrella stages have no progress-bar slot but still appear in error banners;
# give them a Bengali label for the generic "stage failed" fallback.
_UMBRELLA_LABELS_BN = {
    "upload_pipeline": "আপলোড ও এক্সট্রাকশন",
    "voiceover_auto": "অটো ভয়েসওভার",
    "user_audio_pipeline": "অডিও প্রসেসিং",
    "auto_full_render": "ফুল-অটো রেন্ডার",
    "resume": "রিজিউম",
    "final_render": "ফাইনাল রেন্ডার",
    "final_assembly": "চূড়ান্ত ভিডিও একত্রকরণ",
}

# ``RunStageError`` style messages the pipeline raises for ffmpeg/ffprobe
# failures (see auto_cut._run / voiceover_auto._run / video_ingest).
_FFMPEG_HINTS = ("ffmpeg", "ffprobe")

# Messages that mean the job is waiting on the network / a remote service.
_TIMEOUT_HINTS = ("timed out", "timeout", "connection", "network", "socket")

_BUDGET_BN = (
    "এই জবটি শেষ হওয়ার আগেই Gemini কল-বাজেট শেষ হয়ে গেছে "
    "({used}/{max_calls} কল ব্যবহৃত)। নতুন বাজেট দিয়ে আবার চেষ্টা করুন, "
    "অথবা Settings-এ MAX_API_CALLS_PER_JOB বাড়ান।"
)

_KEYS_EXHAUSTED_BN = (
    "কনফিগার করা সব {n} টি Gemini API key ব্যর্থ হয়েছে — রেট লিমিট বা কোটা "
    "শেষ (সবশেষ: {reason})। দৈনিক কোটা রিসেট হওয়া পর্যন্ত অপেক্ষা করে আবার "
    "চেষ্টা করুন, অথবা Settings থেকে আরও key যোগ করুন।"
)

_FFMPEG_BN = (
    "ভিডিও/অডিও প্রসেসিং (ffmpeg) ব্যর্থ হয়েছে। ফাইলটি দূষিত বা অসমর্থিত "
    "হতে পারে — অন্য ফাইল দিয়ে চেষ্টা করুন।"
)

_WHISPER_BN = (
    "লোকাল Whisper ট্রান্সক্রিপশন কাজ করছে না। Whisper ইনস্টল নেই বা মডেল "
    "লোড হয়নি — চাইলে Settings-এ \"Gemini only\" ইঞ্জিন বেছে নিন।"
)

_FORMAT_BN = (
    "সাবটাইটেল ডেটার ফরম্যাট সঠিক নয় (F12-এ পূর্ণ বৈধতা আসছে)। "
    "ফাইলটি এখানে রাখুন এবং আবার চেষ্টা করুন।"
)

_TIMEOUT_BN = (
    "নেটওয়ার্ক/টাইমআউট সমস্যা হয়েছে। ইন্টারনেট সংযোগ ঠিক আছে কিনা দেখে "
    "আবার চেষ্টা করুন।"
)

_GENERIC_BN = "'{stage}' ধাপ চলাকালীন সমস্যা হয়েছে: {detail}"

_UNKNOWN_BN = "অজানা ত্রুটি হয়েছে। আবার চেষ্টা করুন।"


def _stage_label_zh(stage):
    """Bengali label for a stage name, falling back to the raw name."""
    if not stage:
        return "অজানা"
    label = stages.STAGE_LABELS_BN.get(stage) or _UMBRELLA_LABELS_BN.get(stage)
    return label or stage


def _truncate(text, limit=280):
    text = str(text or "").strip()
    first_line = text.splitlines()[0] if text else ""
    if len(first_line) > limit:
        return first_line[:limit] + "…"
    return first_line


def _is_whisper_failure(exc):
    if isinstance(exc, ImportError) and "whisper" in str(exc).lower():
        return True
    if type(exc).__module__.startswith("whisper"):
        return True
    return "whisper" in str(exc).lower()


def _is_ffmpeg_failure(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        return True
    text = str(exc).lower()
    return any(hint in text for hint in _FFMPEG_HINTS)


def _is_timeout_failure(exc):
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
        return True
    text = str(exc).lower()
    return any(hint in text for hint in _TIMEOUT_HINTS)


def _is_format_failure(exc):
    if isinstance(exc, json.JSONDecodeError):
        return True
    if isinstance(exc, ValueError):
        text = str(exc).lower()
        return ("transcript" in text or "subtitle" in text or "format" in text)
    return False


def explain_bn(exc, stage=None):
    """Return a Bengali summary of a pipeline failure; never raises."""
    try:
        if isinstance(exc, gemini_rotation.CallBudgetExceeded):
            return _BUDGET_BN.format(used=exc.used, max_calls=exc.max_calls)
        if isinstance(exc, gemini_rotation.AllKeysExhausted):
            n = len(exc.attempts)
            if n == 0:
                return "কোনো সক্রিয় Gemini API key নেই। Settings থেকে একটা key যোগ করুন।"
            return _KEYS_EXHAUSTED_BN.format(n=n, reason=exc.attempts[-1][1])
        if _is_ffmpeg_failure(exc):
            return _FFMPEG_BN
        if _is_whisper_failure(exc):
            return _WHISPER_BN
        if _is_format_failure(exc):
            return _FORMAT_BN
        if _is_timeout_failure(exc):
            return _TIMEOUT_BN
        return _GENERIC_BN.format(
            stage=_stage_label_zh(stage), detail=_truncate(exc)
        )
    except Exception:  # noqa: BLE001 - the Bengali mirror must never raise
        return _UNKNOWN_BN
