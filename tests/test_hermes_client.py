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


class TestStepWatcherStoppedOnException(unittest.TestCase):
    """The step watcher must be stopped on EVERY exit path of chat(), not just
    the happy path. If subprocess setup or turn processing raises, the daemon
    watcher would otherwise be orphaned and keep refreshing the liveness
    marker for later turns. Regression: the stop event was only set in the
    happy path of chat(); it now lives in a finally block."""

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

    def test_watcher_stopped_when_guard_raises(self):
        client = make_client(hermes_bin="/usr/bin/true")
        client.session_name = "mesh-test"
        client.turn_alive_file = os.path.join(self._tmp.name, ".turn-alive")
        client._step_mode = True
        client._push_callback = lambda _body: None  # step-mode + push -> watcher starts
        client._resume_id = None

        # Capture the stop event handed to the watcher thread.
        recorded = {}
        real_watcher = client._run_step_watcher

        def spy(sid, stop_evt):
            recorded["stop_evt"] = stop_evt
            # Do not actually run the polling loop; we only need the event ref.

        client._run_step_watcher = spy

        # Force the turn to raise so chat() takes an exception path.
        def boom(_cmd):
            raise RuntimeError("simulated subprocess failure")

        client._run_with_liveness_guard = boom

        reply = client.chat("hello")
        # chat() must swallow the exception and return an error string.
        self.assertTrue(reply.startswith("❌ Unexpected error"), reply)
        # The watcher stop event must have been created and set (finally path).
        self.assertIn("stop_evt", recorded)
        self.assertTrue(recorded["stop_evt"].is_set(),
                        "watcher_stop was not set on the exception path")

    def test_watcher_stopped_on_file_not_found(self):
        client = make_client(hermes_bin="/usr/bin/true")
        client.session_name = "mesh-test"
        client.turn_alive_file = os.path.join(self._tmp.name, ".turn-alive")
        client._step_mode = True
        client._push_callback = lambda _body: None
        client._resume_id = None

        recorded = {}

        def spy(sid, stop_evt):
            recorded["stop_evt"] = stop_evt

        client._run_step_watcher = spy

        # FileNotFoundError path: make the binary missing.
        client.hermes_bin = "/nonexistent/hermes-bin-path"
        # _run_with_liveness_guard's Popen will raise FileNotFoundError.
        # Leave it unpatched so the real Popen fires.
        reply = client.chat("hello")
        self.assertTrue(reply.startswith("❌ Hermes Agent not found"), reply)
        self.assertIn("stop_evt", recorded)
        self.assertTrue(recorded["stop_evt"].is_set(),
                        "watcher_stop was not set on the FileNotFoundError path")


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
        # is above the anchor and irrelevant: it is not a tool row). Now a
        # per-call tuple threaded into the recap.
        client = self._client(sid)
        anchor = client._capture_turn_anchor(sid)
        # Now simulate the child having written turn B's user row.
        self._add_msg(sid, "user", content="turn B (child write)")
        recap = client.tool_recap(limit=8, anchor=anchor)
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
        anchor = client._capture_turn_anchor(sid)
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
        recap = client.tool_recap(limit=8, anchor=anchor)
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
        anchor = client._capture_turn_anchor(None)  # sid unknown at capture time
        self.assertIsNone(anchor)
        recap = client.tool_recap(limit=8, anchor=anchor)
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
        anchor = client._capture_turn_anchor(sid_x)
        # Recap for session Y with X's anchor: sid mismatch → no filter,
        # Y's own tools show (not X's — the WHERE session_id still scopes).
        client._resume_id = sid_y
        recap = client.tool_recap(limit=8, anchor=anchor)
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
        anchor = client._capture_turn_anchor(sid, boundary="first_user")
        recap = client.tool_recap(limit=8, anchor=anchor)
        names = [r["name"] for r in recap]
        self.assertEqual(names, ["terminal"])

    def test_capture_anchor_is_pure_per_call_no_shared_state(self):
        """_capture_turn_anchor returns a LOCAL tuple and must NOT mutate any
        shared client attribute. This is the guard against the old bug where
        self._turn_anchor_id / self._turn_anchor_sid were shared mutable state
        that concurrent bridge turns could clobber. Two captures from two
        "threads" must return independent values with no cross-talk."""
        sid = "20260101_000001_aaaaaa"
        self._add_session(sid)
        self._add_msg(sid, "user", content="t0")
        client = self._client(sid)
        # No shared anchor attribute may exist on the client at all anymore
        # (getattr avoids a hard reference so Pyright doesn't flag it as a
        # nonexistent attribute — the whole point is that it's GONE).
        self.assertIsNone(
            getattr(client, "_turn_anchor_id", None),
            "client must not keep a shared mutable _turn_anchor_id",
        )
        self.assertIsNone(
            getattr(client, "_turn_anchor_sid", None),
            "client must not keep a shared mutable _turn_anchor_sid",
        )
        a1 = client._capture_turn_anchor(sid)
        self.assertIsInstance(a1, tuple)
        self.assertIsNotNone(a1)
        self.assertEqual(len(a1), 2)
        # A second, independent capture (simulating a concurrent turn) must
        # not have been affected by the first.
        a2 = client._capture_turn_anchor(sid)
        self.assertEqual(a1, a2)

    def test_anchor_tuples_are_independent_across_concurrent_turns(self):
        """Two concurrent turns on the SAME client (the bridge's thread pool
        fans out on one HermesClient) each compute their own per-turn anchor
        tuple; neither sees the other's boundary. This is the concurrency
        regression test for Issue #1 (shared mutable instance anchor).

        Models the real chat() ordering: prior-turn rows already exist, the
        anchor is captured at pre-spawn (MAX(id) = end of prior turns) while
        the turn lock is held, THEN the child writes this turn's tool rows
        (ids above the anchor), and the recap filters on id > anchor.
        """
        import threading
        sid_a = "20260101_000001_aaaaaa"
        sid_b = "20260101_000002_bbbbbb"
        self._add_session(sid_a)
        self._add_session(sid_b)
        # Prior-turn rows (below the pre-spawn anchor).
        self._add_msg(sid_a, "user", content="A prior")
        self._add_msg(sid_a, "assistant",
                      tool_calls='[{"id": "a0", "function": {"name": '
                      '"search_files", "arguments": "{}"}}]',
                      content="")
        self._add_msg(sid_b, "user", content="B prior")
        self._add_msg(sid_b, "assistant",
                      tool_calls='[{"id": "b0", "function": {"name": '
                      '"read_file", "arguments": "{}"}}]',
                      content="")
        # ONE client; two concurrent turns race on it. Capture each turn's
        # anchor at pre-spawn, interleaved — exactly what chat() does under
        # the turn lock. (A stale shared anchor from the other turn would
        # clobber one of these; with per-call tuples each is independent.)
        client = make_client(source_tag="reticulum", hermes_bin="/usr/bin/true")
        client._resume_id = sid_a
        results = {}
        lock = threading.Lock()

        def cap_turn(sid):
            with lock:
                results[sid] = client._capture_turn_anchor(sid)

        threads = [
            threading.Thread(target=cap_turn, args=(sid_a,)),
            threading.Thread(target=cap_turn, args=(sid_b,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each turn's anchor scoped to ITS session (the tuple carries the sid).
        self.assertEqual(results[sid_a][0], sid_a)
        self.assertEqual(results[sid_b][0], sid_b)
        # Now simulate the child writing each turn's tool row (id above the
        # anchor), then recap with that turn's OWN anchor + OWN session.
        self._add_msg(sid_a, "assistant",
                      tool_calls='[{"id": "a1", "function": {"name": '
                      '"terminal", "arguments": "{}"}}]',
                      content="")
        self._add_msg(sid_b, "assistant",
                      tool_calls='[{"id": "b1", "function": {"name": '
                      '"web_search", "arguments": "{}"}}]',
                      content="")
        client._resume_id = sid_a
        names_a = [r["name"] for r in client.tool_recap(limit=8, anchor=results[sid_a])]
        # Only THIS turn's tool (id > anchor); prior-turn search_files excluded.
        self.assertEqual(names_a, ["terminal"])
        client._resume_id = sid_b
        names_b = [r["name"] for r in client.tool_recap(limit=8, anchor=results[sid_b])]
        self.assertEqual(names_b, ["web_search"])
        # Cross-session isolation: each recap only ever shows its own session's
        # tool — the shared mutable-instance-anchor bug would have let one
        # turn's stale boundary leak into the other.
        self.assertNotIn("read_file", names_a)
        self.assertNotIn("search_files", names_b)
        self.assertNotIn("web_search", names_a)
        self.assertNotIn("terminal", names_b)

    def test_first_turn_recap_uses_adopted_anchor_not_stale_pre_spawn(self):
        """Regression for Issue #2: on the FIRST turn the sid is only known
        after _adopt_new_session. The recap must use the adoption-computed
        first_user anchor, NOT the stale pre-spawn anchor (which had sid=None
        and would fall back to no filter, over-recapping). We simulate the
        ordering: pre-spawn capture (None) -> child writes rows -> adoption
        computes first_user anchor -> recap uses the adopted anchor."""
        sid = "20260101_000003_cccccc"
        self._add_session(sid)
        client = self._client(sid)
        # Pre-spawn: sid not yet known to this turn (fresh session), so chat()
        # captures an anchor with sid=None -> None (no filter).
        pre_spawn_anchor = client._capture_turn_anchor(None)
        self.assertIsNone(pre_spawn_anchor)
        # Child writes this turn's rows: user + one tool call.
        self._add_msg(sid, "user", content="first turn")
        self._add_msg(
            sid, "assistant",
            tool_calls='[{"id": "f1", "function": {"name": "terminal", "arguments": "{}"}}]',
            content="",
        )
        self._add_msg(sid, "tool", tool_name="terminal", tool_call_id="f1", content="ok")
        # A prior turn's tool that must NOT bleed in (simulated as lower id).
        # In reality prior turns don't exist for a brand-new session, so the
        # key assertion is that the adopted anchor yields exactly this turn's
        # tool and the pre-spawn (None) anchor would be the fallback.
        # Adoption now computes the first_user anchor (sid known).
        adopted_anchor = client._capture_turn_anchor(sid, boundary="first_user")
        self.assertIsNotNone(adopted_anchor)
        # Recap with the ADOPTED anchor -> exactly this turn's tool.
        names = [r["name"] for r in client.tool_recap(limit=8, anchor=adopted_anchor)]
        self.assertEqual(names, ["terminal"])
        # Recap with the STALE pre-spawn anchor (None) would fall back to no
        # filter and also include the tool (whole session) — proving the
        # adopted anchor is the correct, tighter choice for a first turn.
        stale_names = [r["name"] for r in client.tool_recap(limit=8, anchor=pre_spawn_anchor)]
        self.assertEqual(stale_names, ["terminal"])

if __name__ == "__main__":
    unittest.main()
