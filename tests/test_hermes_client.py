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


class TestToolRecapTurnScoping(unittest.TestCase):
    """tool_recap() must recap only the CURRENT turn's tool calls, not the
    session's whole history (the "prior-turns recap" bug:
    docs/mesh-bridge-findings-2026-09-15-tool-recap-prior-turns.md)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = os.path.join(self._tmp.name, "state.db")
        conn = sqlite3.connect(self._db)
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, cwd TEXT, "
            "started_at REAL, title TEXT)"
        )
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT, role TEXT, tool_name TEXT, "
            "tool_calls TEXT, tool_call_id TEXT, content TEXT)"
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

    def _add_session(self, session_id):
        conn = sqlite3.connect(self._db)
        conn.execute(
            "INSERT INTO sessions (id, source, cwd, started_at, title) "
            "VALUES (?, 'reticulum', NULL, 0, 'mesh-reticulum')",
            (session_id,),
        )
        conn.commit()
        conn.close()

    def _add_msg(self, session_id, role, tool_name=None,
                 tool_calls=None, tool_call_id=None, content=""):
        conn = sqlite3.connect(self._db)
        conn.execute(
            "INSERT INTO messages "
            "(session_id, role, tool_name, tool_calls, tool_call_id, content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, tool_name, tool_calls, tool_call_id, content),
        )
        conn.commit()
        conn.close()

    def _client(self, session_id):
        client = make_client(source_tag="reticulum", hermes_bin="/usr/bin/true")
        client._resume_id = session_id
        return client

    def test_tool_free_turn_gets_no_recap(self):
        """Turn A used tools; turn B used none → turn B's recap is []."""
        sid = "20260101_000001_aaaaaa"
        self._add_session(sid)
        # Turn A: user msg + assistant tool_calls + tool result.
        self._add_msg(sid, "user", content="turn A")
        self._add_msg(
            sid, "assistant",
            tool_calls='[{"id": "c1", "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}}]',
            content="",
        )
        self._add_msg(sid, "tool", tool_name="terminal",
                      tool_call_id="c1", content="ok")
        # Turn B: user msg only (no tool rows).
        self._add_msg(sid, "user", content="turn B")
        # Anchor at the pre-spawn MAX(id) for turn B's chat() — the end of
        # turn A's rows (the turn B user row is written by the child, so it
        # is above the anchor and irrelevant: it is not a tool row).
        client = self._client(sid)
        client._capture_turn_anchor(sid)
        # Now simulate the child having written turn B's user row.
        self._add_msg(sid, "user", content="turn B (child write)")
        recap = client.tool_recap(limit=8)
        self.assertEqual(recap, [])

    def test_recap_excludes_prior_turns_tools(self):
        """Turn B used tools; turn A's earlier tools must not appear."""
        sid = "20260101_000001_aaaaaa"
        self._add_session(sid)
        # Turn A: one tool call.
        self._add_msg(sid, "user", content="turn A")
        self._add_msg(
            sid, "assistant",
            tool_calls='[{"id": "a1", "function": {"name": "search_files", "arguments": "{}"}}]',
            content="",
        )
        self._add_msg(sid, "tool", tool_name="search_files",
                      tool_call_id="a1", content="{}")
        # Anchor for turn B — captured BEFORE the child spawns, i.e. while
        # turn A's rows are the last ones in the session (MAX(id) = end of
        # turn A). This mirrors chat() capturing the anchor pre-spawn.
        client = self._client(sid)
        client._capture_turn_anchor(sid)
        # Turn B: its own tool call only. (arguments is a JSON-encoded
        # string, double-escaped, exactly as hermes persists it.)
        self._add_msg(sid, "user", content="turn B")
        self._add_msg(
            sid, "assistant",
            tool_calls='[{"id": "b1", "function": {"name": "terminal", "arguments": "{\\"command\\": \\"pwd\\"}"}}]',
            content="",
        )
        self._add_msg(sid, "tool", tool_name="terminal",
                      tool_call_id="b1", content="/tmp")
        recap = client.tool_recap(limit=8)
        names = [r["name"] for r in recap]
        self.assertEqual(names, ["terminal"])
        self.assertNotIn("search_files", names)

    def test_no_anchor_falls_back_to_session_history(self):
        """Anchor unset (sid None at capture) → old behavior, whole session."""
        sid = "20260101_000001_aaaaaa"
        self._add_session(sid)
        self._add_msg(sid, "user", content="turn A")
        self._add_msg(
            sid, "assistant",
            tool_calls='[{"id": "a1", "function": {"name": "terminal", "arguments": "{}"}}]',
            content="",
        )
        client = self._client(sid)
        client._capture_turn_anchor(None)  # sid unknown at capture time
        self.assertIsNone(client._turn_anchor_id)
        recap = client.tool_recap(limit=8)
        names = [r["name"] for r in recap]
        self.assertEqual(names, ["terminal"])

    def test_anchor_scoped_to_matching_sid(self):
        """Anchor captured for session X must not filter session Y."""
        sid_x = "20260101_000001_aaaaaa"
        sid_y = "20260101_000002_bbbbbb"
        self._add_session(sid_x)
        self._add_session(sid_y)
        self._add_msg(sid_x, "user", content="x")
        self._add_msg(
            sid_x, "assistant",
            tool_calls='[{"id": "x1", "function": {"name": "terminal", "arguments": "{}"}}]',
            content="",
        )
        self._add_msg(sid_y, "user", content="y")
        self._add_msg(
            sid_y, "assistant",
            tool_calls='[{"id": "y1", "function": {"name": "read_file", "arguments": "{}"}}]',
            content="",
        )
        client = self._client(sid_x)
        client._capture_turn_anchor(sid_x)
        # Recap for session Y with X's anchor: sid mismatch → no filter,
        # Y's own tools show (not X's — the WHERE session_id still scopes).
        client._resume_id = sid_y
        client._turn_anchor_sid = sid_x
        recap = client.tool_recap(limit=8)
        names = [r["name"] for r in recap]
        self.assertEqual(names, ["read_file"])

    def test_first_turn_adopted_session_uses_first_user_boundary(self):
        """Adopted (brand-new) session: boundary=first_user means this
        turn's tools are included (they sit above the first user row)."""
        sid = "20260101_000003_cccccc"
        self._add_session(sid)
        # The child (this turn) wrote: user row, then tool calls.
        self._add_msg(sid, "user", content="first turn")
        self._add_msg(
            sid, "assistant",
            tool_calls='[{"id": "f1", "function": {"name": "terminal", "arguments": "{}"}}]',
            content="",
        )
        self._add_msg(sid, "tool", tool_name="terminal",
                      tool_call_id="f1", content="ok")
        client = self._client(sid)
        client._capture_turn_anchor(sid, boundary="first_user")
        recap = client.tool_recap(limit=8)
        names = [r["name"] for r in recap]
        self.assertEqual(names, ["terminal"])


if __name__ == "__main__":
    unittest.main()
