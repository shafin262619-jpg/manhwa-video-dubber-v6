"""Tests for pipeline.gemini_rotation (U2a — error classification + budget)."""

import unittest

from pipeline import gemini_rotation
from pipeline.gemini_rotation import (
    AllKeysExhausted,
    CallBudget,
    CallBudgetExceeded,
    ContentSafetyBlocked,
    GeminiRotationError,
    NonRotatableError,
    call_with_rotation_v2,
    classify_error,
)


class _FlipFlop:
    """Fake callable that fails N times then succeeds (rotatable errors)."""

    def __init__(self, fail_times=0, exc=RuntimeError("boom")):
        self.fail_times = fail_times
        self.exc = exc
        self.calls = []
        self._failures = 0

    def __call__(self, key, *args):
        self.calls.append(key)
        if self._failures < self.fail_times:
            self._failures += 1
            raise self.exc
        return f"ok:{key}"


class _NeverOk:
    """Fake callable that always raises the given exception."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    def __call__(self, key, *args):
        self.calls.append(key)
        raise self.exc


class ClassifyErrorTest(unittest.TestCase):
    def test_rotatable_markers(self):
        for msg in (
            "HTTP 429 Too Many Requests",
            "429",
            "quota exceeded",
            "quota",
            "rate limit hit",
            "rate limit",
            "request timed out",
            "timeout",
            "connection reset",
            "connection",
            "service unavailable 503",
            "503",
            "bad gateway 502",
            "502",
            "internal server error 500",
            "500",
        ):
            with self.subTest(msg=msg):
                self.assertEqual(
                    classify_error(RuntimeError(msg)), "rotatable", msg
                )

    def test_non_rotatable_markers(self):
        for msg in (
            "400 Bad Request",
            "400",
            "invalid argument",
            "invalid",
            "content blocked",
            "content",
            "safety violation",
            "safety",
            "request blocked",
            "blocked",
        ):
            with self.subTest(msg=msg):
                self.assertEqual(
                    classify_error(RuntimeError(msg)), "non_rotatable", msg
                )

    def test_default_is_rotatable(self):
        for msg in ("some random message", "", "server hiccup", "EIO"):
            with self.subTest(msg=msg):
                self.assertEqual(
                    classify_error(RuntimeError(msg)), "rotatable", msg
                )

    def test_non_rotatable_error_instance_always_non_rotatable(self):
        self.assertEqual(
            classify_error(ContentSafetyBlocked("blocked by policy")),
            "non_rotatable",
        )
        self.assertEqual(
            classify_error(NonRotatableError("429 looks transient")),
            "non_rotatable",
        )

    def test_rotatable_wins_when_both_marker_groups_present(self):
        self.assertEqual(
            classify_error(RuntimeError("timeout then content blocked")),
            "rotatable",
        )


class CallBudgetTest(unittest.TestCase):
    def test_consume_increments_until_limit(self):
        budget = CallBudget(max_calls=2)
        self.assertEqual(budget.used, 0)
        budget.consume()
        budget.consume()
        self.assertEqual(budget.used, 2)
        with self.assertRaises(CallBudgetExceeded) as ctx:
            budget.consume()
        self.assertEqual(ctx.exception.used, 2)
        self.assertEqual(ctx.exception.max_calls, 2)

    def test_zero_max_calls_raises_immediately(self):
        budget = CallBudget(max_calls=0)
        with self.assertRaises(CallBudgetExceeded) as ctx:
            budget.consume()
        self.assertEqual(ctx.exception.used, 0)
        self.assertEqual(ctx.exception.max_calls, 0)

    def test_none_means_unlimited(self):
        budget = CallBudget(max_calls=None)
        for _ in range(1000):
            budget.consume()
        self.assertEqual(budget.used, 1000)

    def test_negative_max_calls_rejected(self):
        with self.assertRaises(ValueError):
            CallBudget(max_calls=-1)


class CallWithRotationV2Test(unittest.TestCase):
    def test_success_returns_result_and_next_rotation(self):
        cb = _FlipFlop()
        result, next_rotation = call_with_rotation_v2(
            ["k0", "k1", "k2"], rotation=0, callable_=cb
        )
        self.assertEqual(result, "ok:k0")
        self.assertEqual(next_rotation, 1)
        self.assertEqual(cb.calls, ["k0"])

    def test_rotation_advances_from_given_index(self):
        cb = _FlipFlop()
        result, next_rotation = call_with_rotation_v2(
            ["k0", "k1", "k2"], rotation=2, callable_=cb
        )
        self.assertEqual(result, "ok:k2")
        self.assertEqual(next_rotation, 0)

    def test_rotatable_failure_tries_next_key(self):
        cb = _FlipFlop(fail_times=1, exc=RuntimeError("429 rate limited"))
        result, next_rotation = call_with_rotation_v2(
            ["k0", "k1"], rotation=0, callable_=cb
        )
        self.assertEqual(result, "ok:k1")
        self.assertEqual(next_rotation, 0)
        self.assertEqual(cb.calls, ["k0", "k1"])

    def test_all_keys_fail_raises_all_keys_exhausted_with_attempts(self):
        cb = _NeverOk(RuntimeError("timeout"))
        with self.assertRaises(AllKeysExhausted) as ctx:
            call_with_rotation_v2(["k0", "k1", "k2"], rotation=0, callable_=cb)
        self.assertEqual(
            ctx.exception.attempts,
            [(0, "timeout"), (1, "timeout"), (2, "timeout")],
        )
        self.assertEqual(cb.calls, ["k0", "k1", "k2"])

    def test_attempts_use_rotated_key_indices(self):
        cb = _NeverOk(RuntimeError("timeout"))
        with self.assertRaises(AllKeysExhausted) as ctx:
            call_with_rotation_v2(["k0", "k1", "k2"], rotation=1, callable_=cb)
        self.assertEqual(
            ctx.exception.attempts,
            [(1, "timeout"), (2, "timeout"), (0, "timeout")],
        )

    def test_non_rotatable_error_stops_without_trying_other_keys(self):
        cb = _NeverOk(RuntimeError("content blocked"))
        with self.assertRaises(RuntimeError) as ctx:
            call_with_rotation_v2(["k0", "k1", "k2"], rotation=0, callable_=cb)
        self.assertIn("content blocked", str(ctx.exception))
        self.assertEqual(cb.calls, ["k0"])

    def test_content_safety_blocked_propagates_original_exception(self):
        exc = ContentSafetyBlocked("prohibited content")
        cb = _NeverOk(exc)
        with self.assertRaises(ContentSafetyBlocked) as ctx:
            call_with_rotation_v2(["k0", "k1"], rotation=0, callable_=cb)
        self.assertIs(ctx.exception, exc)
        self.assertEqual(cb.calls, ["k0"])

    def test_non_rotatable_on_second_key_after_first_rotatable(self):
        # First key: rotatable error (records attempt); second key: blocked.
        def fake(key):
            if key == "k0":
                raise RuntimeError("timeout")
            raise RuntimeError("blocked by policy")

        with self.assertRaises(RuntimeError) as ctx:
            call_with_rotation_v2(["k0", "k1", "k2"], rotation=0, callable_=fake)
        self.assertIn("blocked by policy", str(ctx.exception))

    def test_empty_keys_raises_all_keys_exhausted(self):
        with self.assertRaises(AllKeysExhausted) as ctx:
            call_with_rotation_v2([], rotation=0, callable_=_FlipFlop())
        self.assertEqual(ctx.exception.attempts, [])

    def test_call_budget_propagates_budget_exceeded(self):
        budget = CallBudget(max_calls=2)
        cb = _NeverOk(RuntimeError("timeout"))
        with self.assertRaises(CallBudgetExceeded) as ctx:
            call_with_rotation_v2(
                ["k0", "k1", "k2"], rotation=0, callable_=cb,
                call_budget=budget,
            )
        self.assertEqual(ctx.exception.used, 2)
        self.assertEqual(ctx.exception.max_calls, 2)
        self.assertEqual(cb.calls, ["k0", "k1"])

    def test_call_budget_counts_successful_calls_too(self):
        budget = CallBudget(max_calls=1)
        cb = _FlipFlop(fail_times=1, exc=RuntimeError("timeout"))
        with self.assertRaises(CallBudgetExceeded):
            call_with_rotation_v2(
                ["k0", "k1"], rotation=0, callable_=cb, call_budget=budget
            )
        self.assertEqual(cb.calls, ["k0"])
        self.assertEqual(budget.used, 1)

    def test_no_budget_means_unlimited(self):
        cb = _NeverOk(RuntimeError("timeout"))
        with self.assertRaises(AllKeysExhausted):
            call_with_rotation_v2(["k0", "k1", "k2"], rotation=0, callable_=cb)

    def test_args_forwarded_to_callable(self):
        captured = []

        def fake(key, arg):
            captured.append((key, arg))
            return "ok"

        result, _ = call_with_rotation_v2(["k0"], 0, fake, "a")
        self.assertEqual(captured, [("k0", "a")])
        self.assertEqual(result, "ok")

    def test_success_after_rotation_records_no_attempts_leak(self):
        cb = _FlipFlop(fail_times=1, exc=RuntimeError("timeout"))
        result, next_rotation = call_with_rotation_v2(
            ["k0", "k1", "k2"], rotation=2, callable_=cb
        )
        self.assertEqual(result, "ok:k0")
        self.assertEqual(next_rotation, 1)
        self.assertEqual(cb.calls, ["k2", "k0"])


if __name__ == "__main__":
    unittest.main()
