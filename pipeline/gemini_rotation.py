"""Gemini key-rotation core: error classification + call budget.

Chunk U2a of the robustness update plan (U0–U5). This module is standalone
and deliberately *unwired*: it defines the building blocks that the existing
call sites (``subtitle_extract.call_with_rotation``, ``translator``,
``voiceover_auto``) will swap over to in chunk U2b. No existing pipeline code
is touched here.

The v1 ``call_with_rotation`` returns ``(result, next_rotation, error)``
with an error dict on failure. ``call_with_rotation_v2`` keeps the same
success signature ``(result, next_rotation)`` (first two elements are
identical, so U2b can swap call sites easily) but signals failure with
exceptions instead of an error dict:

- ``NonRotatableError`` family (e.g. content safety blocks): raise
  immediately, do not try another key — rotating cannot help.
- ``AllKeysExhausted``: every key was tried and failed; carries the per-key
  attempt log ``[(key_index, reason), ...]``.
- ``CallBudgetExceeded``: the per-job call budget ran out; propagated to the
  caller so it can abort before burning quota.

Classification is deliberately conservative: unknown errors default to
``rotatable`` because trying the next key costs only a little time, whereas
stopping a recoverable call early costs a whole job.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Google's SDK stringifies a quota-exceeded exception as a large nested
# dict (full HTTP error body, help links, per-violation quota metadata).
# That is worth keeping in full in the per-job log file for debugging, but
# printing it in full to the console on *every single rotated key* turns
# one 429 into a multi-thousand-character wall of unreadable text (this is
# the exact failure mode that made the console output unreadable when every
# key hit the same quota error back-to-back). Truncate what actually reaches
# the log line; the full text is still available via ``exc`` to any caller
# that wants it (e.g. ``AllKeysExhausted.attempts`` keeps the untruncated
# reason for each key).
_LOG_ERROR_PREVIEW_CHARS = 160


def _short(text: str, limit: int = _LOG_ERROR_PREVIEW_CHARS) -> str:
    """First line, truncated to ``limit`` chars, of an exception's message.

    Google's genai SDK errors are multi-line nested-dict reprs; the useful
    part (status + short reason) is always at the very start, so keeping
    just the first line and a hard char cap drops the repeated
    boilerplate (help links, per-violation quota metadata) without losing
    the actual reason.
    """
    first_line = text.strip().splitlines()[0] if text.strip() else text
    if len(first_line) > limit:
        return first_line[:limit].rstrip() + "…"
    return first_line


class GeminiRotationError(Exception):
    """Base class for all errors raised by this rotation module."""


class NonRotatableError(GeminiRotationError):
    """An error where switching keys cannot help (base class).

    Subclasses carry the specific reason. ``call_with_rotation_v2`` stops
    immediately (re-raises the original exception) when classification marks
    a failure as non-rotatable.
    """


class ContentSafetyBlocked(NonRotatableError):
    """The Gemini content-safety policy blocked the request.

    Key-independent: no amount of rotation will change the outcome, so the
    caller should surface it to the user rather than retrying keys.
    """


class AllKeysExhausted(GeminiRotationError):
    """Every active Gemini key failed for a rotatable reason.

    ``.attempts`` is the ordered log of failures:
    ``[(key_index: int, reason: str), ...]`` — one entry per key tried.
    """

    def __init__(self, attempts):
        self.attempts = list(attempts)
        super().__init__(
            f"All {len(self.attempts)} Gemini key(s) failed: "
            f"{[(idx, reason) for idx, reason in self.attempts]}"
        )


class CallBudgetExceeded(GeminiRotationError):
    """The per-job Gemini call budget ran out.

    Carries the number of calls used and the configured maximum so the caller
    can report and/or decide what to do next.
    """

    def __init__(self, used, max_calls):
        self.used = used
        self.max_calls = max_calls
        super().__init__(
            f"Gemini call budget exceeded: {used}/{max_calls} calls used"
        )


# Substrings that mark an error as worth retrying on another key. Checked in
# this order: rotatable markers first, then non-rotatable markers, then a
# ``rotatable`` default.
_ROTATABLE_MARKERS = (
    "429",
    "quota",
    "rate limit",
    "timeout",
    "connection",
    "503",
    "502",
    "500",
)
_NON_ROTATABLE_MARKERS = (
    "400",
    "invalid",
    "content",
    "safety",
    "blocked",
)


def classify_error(exc: Exception) -> str:
    """Classify an exception as ``"rotatable"`` or ``"non_rotatable"``.

    Rules (on ``str(exc).lower()``):

    - contains ``429`` / ``quota`` / ``rate limit`` / ``timeout`` /
      ``connection`` / ``503`` / ``502`` / ``500`` -> ``"rotatable"``
    - contains ``400`` / ``invalid`` / ``content`` / ``safety`` / ``blocked``
      -> ``"non_rotatable"``
    - anything else -> ``"rotatable"`` (safe default: trying the next key is
      cheap, while aborting a recoverable call loses the whole job)

    Instances of :class:`NonRotatableError` (or its subclasses such as
    :class:`ContentSafetyBlocked`) are always ``"non_rotatable"`` regardless
    of message text, since that is the semantic contract of the class.
    """
    if isinstance(exc, NonRotatableError):
        return "non_rotatable"
    text = str(exc).lower()
    if any(marker in text for marker in _ROTATABLE_MARKERS):
        return "rotatable"
    if any(marker in text for marker in _NON_ROTATABLE_MARKERS):
        return "non_rotatable"
    return "rotatable"


class CallBudget:
    """Per-job cap on real Gemini calls.

    ``consume()`` must be called right before every actual Gemini attempt.
    When the budget is exhausted it raises :class:`CallBudgetExceeded`;
    otherwise it increments the internal counter. Not thread-safe by design —
    in this project each rotation call runs sequentially inside a single
    job thread.
    """

    def __init__(self, max_calls: int | None):
        if max_calls is not None and max_calls < 0:
            raise ValueError("max_calls must be None or >= 0")
        self.max_calls = max_calls
        self._used = 0

    @property
    def used(self) -> int:
        return self._used

    def consume(self) -> None:
        if self.max_calls is not None and self._used >= self.max_calls:
            raise CallBudgetExceeded(self._used, self.max_calls)
        self._used += 1


def call_with_rotation_v2(
    keys: list[str],
    rotation: int,
    callable_,
    *args,
    call_budget: CallBudget | None = None,
):
    """Round-robin Gemini key rotation (v2, exception-based).

    Mirrors ``subtitle_extract.call_with_rotation``'s round-robin order and
    returns ``(result, next_rotation)`` on success (the same first two
    elements as v1, so U2b can swap call sites easily).

    Behavior:

    - Every attempt first calls ``call_budget.consume()`` when a budget is
      given; :class:`CallBudgetExceeded` is *not* caught here and propagates
      to the caller.
    - Each key is invoked as ``callable_(key, *args)``.
    - On failure the error is classified with :func:`classify_error`:
      ``"non_rotatable"`` -> the original exception is re-raised immediately
      (no further keys tried); ``"rotatable"`` -> ``(key_index, str(exc))``
      is appended to the attempt log and the next key is tried.
    - When every key fails, :class:`AllKeysExhausted` is raised carrying the
      attempt log as ``.attempts``.
    """
    n = len(keys)
    if n == 0:
        raise AllKeysExhausted([])
    attempts = []
    for attempt in range(n):
        idx = (rotation + attempt) % n
        if call_budget is not None:
            call_budget.consume()
        try:
            result = callable_(keys[idx], *args)
        except Exception as exc:  # noqa: BLE001 - resilience: try next key
            category = classify_error(exc)
            if category == "non_rotatable":
                raise
            reason = _short(str(exc))
            attempts.append((idx, reason))
            logger.warning(
                "Gemini call failed (key idx %d), rotating: %s", idx, reason
            )
            continue
        logger.info("Gemini call OK (key idx %d)", idx)
        return result, (idx + 1) % n
    raise AllKeysExhausted(attempts)
