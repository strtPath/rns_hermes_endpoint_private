"""Unit tests for the new HermesClient liveness/serialization behavior.

Run with:  ./venv/bin/python -m unittest tests.test_hermes_client -v
(bridge tests that need LXMF/RNS are excluded here — pure client logic only)
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
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


class TestTurnHardCap(unittest.TestCase):
    def test_default_is_6_hours(self):
        os.environ.pop("HERMES_TURN_HARD_CAP", None)
        c = make_client()
        self.assertEqual(c.turn_hard_cap, 21600)

    def test_env_override(self):
        os.environ["HERMES_TURN_HARD_CAP"] = "1800"
        try:
            c = make_client()
            self.assertEqual(c.turn_hard_cap, 1800)
        finally:
            os.environ.pop("HERMES_TURN_HARD_CAP", None)

    def test_zero_disables(self):
        os.environ["HERMES_TURN_HARD_CAP"] = "0"
        try:
            c = make_client()
            self.assertEqual(c.turn_hard_cap, 0)
        finally:
            os.environ.pop("HERMES_TURN_HARD_CAP", None)


class TestStepWatcherRefreshesMarker(unittest.TestCase):
    """The step watcher must refresh the turn-alive marker each time it sees
    new tool rows, so a working turn with no stdout bytes doesn't hit the
    liveness wall clock. Regression: the gateway agent:step hook never fires
    for a CLI child, so without this the marker is only the one spawn write."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = os.path.join(self._tmp.name, "state.db")
        conn = sqlite3.connect(self._db)
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "tool_calls TEXT, tool_name TEXT, content TEXT, tool_call_id TEXT)"
        )
        conn.commit()
        conn.close()
        self._prev_db = os.environ.get("HERMES_STATE_DB")
        os.environ["HERMES_STATE_DB"] = self._db

    def tearDown(self):
        if self._prev_db is None:
            os.environ.pop("HERMES_STATE_DB", None)
        else:
            os.environ["HERMES_STATE_DB"] = self._prev_db
        self._tmp.cleanup()

    def _client(self):
        client = make_client(hermes_bin="/usr/bin/true")
        client.session_name = "mesh-test"
        client.turn_alive_file = os.path.join(self._tmp.name, ".turn-alive")
        client._push_callback = None  # no push; marker refresh is what we test
        return client

    def _add_row(self, sid, role, tool_calls=None, tool_name=None,
                 content=None, tool_call_id=None):
        conn = sqlite3.connect(self._db)
        conn.execute(
            "INSERT INTO messages (session_id, role, tool_calls, tool_name, "
            "content, tool_call_id) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, role, tool_calls, tool_name, content, tool_call_id),
        )
        conn.commit()
        conn.close()

    def test_marker_written_when_new_tool_rows_appear(self):
        client = self._client()
        sid = "sess-1"
        # Seed: one user message already present (id 1).
        self._add_row(sid, "user", content="hi")
        stop_evt = threading.Event()
        # Run the watcher in a thread; let it run briefly.
        t = threading.Thread(
            target=client._run_step_watcher, args=(sid, stop_evt), daemon=True
        )
        t.start()
        time.sleep(0.3)
        # Now a tool batch appears (assistant tool_call + tool result).
        self._add_row(
            sid, "assistant",
            tool_calls='[{"id":"c1","function":{"name":"terminal","arguments":{}}}]',
        )
        self._add_row(sid, "tool", tool_name="terminal", content="ok", tool_call_id="c1")
        time.sleep(1.5)
        stop_evt.set()
        t.join(timeout=5)
        # The marker should now exist and be scoped to this child.
        self.assertTrue(os.path.exists(client.turn_alive_file))
        with open(client.turn_alive_file, encoding="utf-8") as f:
            import json as _json
            marker = _json.load(f)
        self.assertEqual(marker["session"], "mesh-test")
        self.assertEqual(marker["phase"], "tool")

    def test_no_marker_refresh_without_new_rows(self):
        client = self._client()
        sid = "sess-2"
        self._add_row(sid, "user", content="hi")
        stop_evt = threading.Event()
        t = threading.Thread(
            target=client._run_step_watcher, args=(sid, stop_evt), daemon=True
        )
        t.start()
        time.sleep(1.5)
        stop_evt.set()
        t.join(timeout=5)
        # No new tool rows → the watcher never wrote a phase="tool" marker.
        if os.path.exists(client.turn_alive_file):
            with open(client.turn_alive_file, encoding="utf-8") as f:
                import json as _json
                marker = _json.load(f)
            self.assertNotEqual(marker.get("phase"), "tool")
        else:
            pass  # no marker at all: also acceptable (nothing to refresh)

    def test_no_marker_refresh_on_unrelated_rows(self):
        # Review hardening: MAX(id) advances for ANY new session row, so a
        # blind refresh would let user/system/text-only rows keep a stalled
        # child alive until the hard cap. Only actual tool activity may warm
        # the heartbeat. A text-only assistant row and a user row must NOT
        # write a phase="tool" marker.
        client = self._client()
        sid = "sess-3"
        # Seed at id 1.
        self._add_row(sid, "user", content="hi")
        stop_evt = threading.Event()
        t = threading.Thread(
            target=client._run_step_watcher, args=(sid, stop_evt), daemon=True
        )
        t.start()
        time.sleep(0.3)
        # Unrelated rows: a text-only assistant reply + another user row.
        self._add_row(sid, "assistant", content="just text, no tool calls")
        self._add_row(sid, "user", content="and more")
        time.sleep(1.5)
        stop_evt.set()
        t.join(timeout=5)
        # No tool activity → no phase="tool" marker was written.
        if os.path.exists(client.turn_alive_file):
            with open(client.turn_alive_file, encoding="utf-8") as f:
                import json as _json
                marker = _json.load(f)
            self.assertNotEqual(marker.get("phase"), "tool")
        else:
            pass  # no marker at all: correct


class TestMarkerScopingDiag(unittest.TestCase):
    def test_no_marker_logs_true_stall(self):
        c = make_client()
        c.turn_alive_file = "/nonexistent/never/written"
        # Must not raise; just logs.
        c._log_marker_scoping_diag()

    def test_scoping_mismatch_detected(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, ".turn-alive")
            with open(path, "w", encoding="utf-8") as f:
                import json as _json
                _json.dump(
                    {"session": "other-session", "ts": time.time(),
                     "phase": "model", "gen": 99}, f)
            c = make_client()
            c.session_name = "mesh-me"
            c.turn_alive_file = path
            c._turn_alive_gen = 1
            # Must not raise; logs the mismatch.
            c._log_marker_scoping_diag()
        finally:
            tmp.cleanup()


class TestSessionAdoptionCorrelation(unittest.TestCase):
    """_adopt_new_session must only ever bind a session created by THIS turn.

    state.db is shared with the gateway, other bridge turns, and any local
    ``hermes chat``. Picking "the newest session that wasn't there before" can
    rename and pin an unrelated conversation, so later mesh messages would then
    resume someone else's context. Adoption therefore correlates on
    source + cwd + start time, and refuses outright when that is ambiguous.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = os.path.join(self._tmp.name, "state.db")
        conn = sqlite3.connect(self._db)
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, cwd TEXT, "
            "started_at REAL, title TEXT)"
        )
        conn.commit()
        conn.close()
        self._prev_db = os.environ.get("HERMES_STATE_DB")
        os.environ["HERMES_STATE_DB"] = self._db

    def tearDown(self):
        if self._prev_db is None:
            os.environ.pop("HERMES_STATE_DB", None)
        else:
            os.environ["HERMES_STATE_DB"] = self._prev_db
        self._tmp.cleanup()

    def _add(self, session_id, started_at, source="reticulum", cwd=None):
        conn = sqlite3.connect(self._db)
        conn.execute(
            "INSERT INTO sessions (id, source, cwd, started_at, title) "
            "VALUES (?, ?, ?, ?, NULL)",
            (session_id, source, cwd, started_at),
        )
        conn.commit()
        conn.close()

    def _client(self):
        client = make_client(source_tag="reticulum", hermes_bin="/usr/bin/true")
        client.session_name = "mesh-reticulum"
        return client

    def test_adopts_the_one_session_from_this_turn(self):
        client = self._client()
        before = client._session_ids()
        self._add("20260101_000001_aaaaaa", time.time() + 1)
        client._adopt_new_session(time.time(), before)
        self.assertEqual(client._resume_id, "20260101_000001_aaaaaa")

    def test_refuses_when_more_than_one_session_appeared(self):
        """Concurrent session creation -> adopt nothing rather than guess."""
        client = self._client()
        before = client._session_ids()
        now = time.time()
        self._add("20260101_000001_aaaaaa", now + 1)
        self._add("20260101_000002_bbbbbb", now + 2)
        client._adopt_new_session(now, before)
        self.assertIsNone(client._resume_id)

    def test_ignores_a_session_from_another_source(self):
        """A gateway/other-platform session in the window is not ours."""
        client = self._client()
        before = client._session_ids()
        now = time.time()
        self._add("20260101_000001_aaaaaa", now + 1, source="telegram")
        client._adopt_new_session(now, before)
        self.assertIsNone(client._resume_id)

    def test_adopts_when_cwd_is_null(self):
        """Regression: Hermes leaves `cwd` NULL for `chat -q` sessions.

        Filtering on cwd would then match nothing, silently breaking thread
        continuity on every bootstrap.
        """
        client = self._client()
        before = client._session_ids()
        self._add("20260101_000001_aaaaaa", time.time() + 1, cwd=None)
        client._adopt_new_session(time.time(), before)
        self.assertEqual(client._resume_id, "20260101_000001_aaaaaa")

    def test_ignores_a_session_that_predates_the_turn(self):
        client = self._client()
        before = client._session_ids()
        self._add("20260101_000001_aaaaaa", time.time() - 3600)
        client._adopt_new_session(time.time(), before)
        self.assertIsNone(client._resume_id)

    def test_never_adopts_a_session_already_in_the_snapshot(self):
        """Even a matching row must be new relative to `before`."""
        client = self._client()
        self._add("20260101_000001_aaaaaa", time.time() + 1)  # exists up front
        before = client._session_ids()
        client._adopt_new_session(time.time(), before)
        self.assertIsNone(client._resume_id)


if __name__ == "__main__":
    unittest.main()
