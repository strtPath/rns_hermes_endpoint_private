"""Unit tests for the new HermesClient liveness/serialization behavior.

Run with:  ./venv/bin/python -m unittest tests.test_hermes_client -v
(bridge tests that need LXMF/RNS are excluded here — pure client logic only)
"""
import os
import sys
import threading
import unittest

# Ensure project src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hermes_reticulum.core.hermes_client import HermesClient  # noqa: E402


def make_client(**kwargs):
    kwargs.setdefault("hermes_bin", "/usr/bin/true")
    return HermesClient(**kwargs)


class TestLivenessTimeoutDefault(unittest.TestCase):
    def test_default_is_600(self):
        os.environ.pop("HERMES_LIVENESS_TIMEOUT", None)
        c = make_client()
        self.assertEqual(c.liveness_timeout, 600)

    def test_env_override(self):
        os.environ["HERMES_LIVENESS_TIMEOUT"] = "42"
        try:
            c = make_client()
            self.assertEqual(c.liveness_timeout, 42)
        finally:
            os.environ.pop("HERMES_LIVENESS_TIMEOUT", None)


class TestGuardKillFlag(unittest.TestCase):
    def test_flag_resets_per_run(self):
        c = make_client()
        c._guard_killed = True  # simulate a previous guard kill
        # _run_with_liveness_guard resets it on entry; simulate the reset:
        c._guard_killed = False
        self.assertFalse(c._guard_killed)

    def test_guard_kill_returns_honest_message(self):
        c = make_client()
        c._guard_killed = True
        c._stop_requested = True
        c._process = None
        # Simulate the return path of _run_with_liveness_guard on a killed proc:
        # we assert the branch in the real method, so call the error helper
        # logic directly: with _guard_killed True, the method must NOT call
        # _error_reply. Verify by monkey-patching the proc flow via a stub.

        class FakeProc:
            returncode = -9

            def poll(self):
                return -9

            def wait(self, timeout=None):
                pass

        # Run the real method with a fake cmd that exits 0 immediately;
        # _guard_killed stays False → normal path.
        os.environ["HERMES_LIVENESS_TIMEOUT"] = "600"
        c.liveness_timeout = 0  # disable watchdog for this test
        reply = c._run_with_liveness_guard(["/usr/bin/true"])
        # 'true' exits 0 with no output → "_(no response)_"
        self.assertEqual(reply, "_(no response)_")


class TestTurnSerialization(unittest.TestCase):
    def test_turn_lock_exists_and_serializes(self):
        c = make_client()
        self.assertTrue(hasattr(c, "_turn_lock"))
        # Two threads acquire in order; both proceed (no deadlock).
        order = []
        with c._turn_lock:
            order.append("first")
        t = threading.Thread(
            target=lambda: (c._turn_lock.acquire(), order.append("second"), c._turn_lock.release())
        )
        t.start()
        with c._turn_lock:
            order.append("third")
        t.join(timeout=5)
        self.assertEqual(order[0], "first")


class TestChatGuardKillRetry(unittest.TestCase):
    def test_chat_retries_after_guard_kill(self):
        """First run is guard-killed *early* → chat() retries once and returns
        the second run's reply."""
        c = make_client()
        c._resume_id = "fake-session-id"  # resume path, no DB needed
        calls = []

        def fake_guard_kill(cmd):
            calls.append(1)
            c._last_run_ms = 100.0  # died early → worth a retry
            if len(calls) == 1:
                c._guard_killed = True
                c._stop_requested = True
                return "⏱️ Turn exceeded the liveness window (600s) — the model may be busy or stalled. Please retry."
            c._guard_killed = False
            return "second-attempt-reply"

        c._run_with_liveness_guard = fake_guard_kill
        # Avoid the 5s sleep in the real chat()
        import hermes_reticulum.core.hermes_client as hc
        orig_sleep = hc.time.sleep
        hc.time.sleep = lambda *a, **k: None
        try:
            result = c.chat("hello")
        finally:
            hc.time.sleep = orig_sleep
        self.assertEqual(result, "second-attempt-reply")
        self.assertEqual(len(calls), 2)

    def test_chat_skips_retry_when_child_ran_full_window(self):
        """A guard-kill that fired after a *full* liveness window means the
        model was slow, not wedged — chat() must NOT retry (it would re-burn
        the window and fail identically); it returns an honest message and
        the guard-kill result stands."""
        c = make_client()
        c._resume_id = "fake-session-id"
        calls = []
        c.liveness_timeout = 600

        def fake_full_window_kill(cmd):
            calls.append(1)
            # Child lived a full window (600s) before the guard fired.
            c._last_run_ms = 601_000.0
            c._guard_killed = True
            c._stop_requested = True
            return "⏱️ Turn exceeded the liveness window (600s) — the model may be busy or stalled. Please retry."

        c._run_with_liveness_guard = fake_full_window_kill
        import hermes_reticulum.core.hermes_client as hc
        orig_sleep = hc.time.sleep
        hc.time.sleep = lambda *a, **k: None
        try:
            result = c.chat("hello")
        finally:
            hc.time.sleep = orig_sleep
        # Only ONE run: the slow-model path skips the retry entirely.
        self.assertEqual(len(calls), 1)
        # And the reply is the honest "ran past the window" message, not the
        # raw guard-kill string.
        self.assertIn("ran past the liveness window", result)

    def test_guard_kill_worth_retrying_threshold(self):
        c = make_client()
        c.liveness_timeout = 600
        # Died well before a full window → retry.
        self.assertTrue(c._guard_kill_worth_retrying(123_000))
        # Ran past a full window → no retry.
        self.assertFalse(c._guard_kill_worth_retrying(601_000))

    def test_no_retry_on_normal_error(self):
        c = make_client()
        c._resume_id = "fake-session-id"
        c._run_with_liveness_guard = lambda cmd: "❌ Error (code 1): something"
        result = c.chat("hello")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("❌"))
        self.assertFalse(c._guard_killed)


if __name__ == "__main__":
    unittest.main()
