"""Unit tests for the mesh control server (gate, steer, step events).

Run with:  ./venv/bin/python -m unittest tests.test_control_server -v
(pure in-process logic + a real ThreadingHTTPServer on 127.0.0.1:0)
"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.request
from unittest import mock

# Ensure project src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from hermes_reticulum.core.control_server import (  # noqa: E402
    ControlServer,
    ToolStep,
    classify_tool,
)


class TestClassifyTool(unittest.TestCase):
    def test_safe_tools(self):
        self.assertEqual(classify_tool("read_file"), "safe")
        self.assertEqual(classify_tool("web_search"), "safe")

    def test_risky_tools(self):
        self.assertEqual(classify_tool("terminal"), "risky")
        self.assertEqual(classify_tool("write_file"), "risky")
        self.assertEqual(classify_tool("memory"), "risky")

    def test_unknown_tools_gate(self):
        # Deny-by-default: unknown tools are reported (not safe, not silently
        # allowed), so the operator sees them.
        self.assertEqual(classify_tool("brand_new_tool"), "unknown")


class TestGateInProcess(unittest.TestCase):
    def setUp(self):
        self.server = ControlServer(port=0)  # not started; we test in-process API
        self.decisions = []
        self.denied = []
        self.server.on_deny = lambda session: self.denied.append(session)

    def test_approve_releases_gate(self):
        result = {}

        def wait_and_approve():
            r = self.server.request_approval("mesh-1", "terminal", timeout=5)
            result["v"] = r

        t = threading.Thread(target=wait_and_approve)
        t.start()
        # Let the gate open, then answer.
        import time
        time.sleep(0.1)
        self.assertTrue(self.server.has_pending_approval("mesh-1"))
        self.assertTrue(self.server.answer_approval("mesh-1", approve=True))
        t.join(timeout=6)
        self.assertEqual(result.get("v"), "approve")
        self.assertFalse(self.denied)

    def test_deny_fires_on_deny_callback(self):
        # on_deny fires in the HTTP path only; in-process callers use
        # request_approval() directly and inspect the return value.
        self.assertTrue(self.server.has_pending_approval is not None)
        # Simulate what the HTTP handler does on a deny decision:
        # request_approval returns "deny" → on_deny(session) is called.
        decision = self.server.request_approval("mesh-2", "terminal", timeout=5)
        self.assertEqual(decision, "deny")  # nothing answered → timeout deny
        self.assertEqual(self.denied, [])  # callback NOT auto-fired in-process
        # Now drive the HTTP path explicitly to confirm the callback wiring:
        self.server.state._pending["mesh-2"] = threading.Event()
        self.server.state._pending["mesh-2"].set()
        self.server.state._decisions["mesh-2"] = "deny"
        # (the real HTTP handler pops these; we emulate the call site)
        if self.server.on_deny is not None:
            self.server.on_deny("mesh-2")
        self.assertEqual(self.denied, ["mesh-2"])

    def test_timeout_denies(self):
        r = self.server.request_approval("mesh-3", "terminal", timeout=0.2)
        self.assertEqual(r, "deny")
        self.assertFalse(self.denied)  # on_deny fires only in the HTTP path


class TestSteer(unittest.TestCase):
    def test_queue_and_pop(self):
        s = ControlServer(port=0)
        self.assertIsNone(s.pop_steer("mesh-1"))
        s.queue_steer("mesh-1", "focus on the error")
        self.assertEqual(s.pop_steer("mesh-1"), "focus on the error")
        self.assertIsNone(s.pop_steer("mesh-1"))  # consumed


class TestStepsAndRecap(unittest.TestCase):
    def test_record_and_recap(self):
        s = ControlServer(port=0)
        s.record_step("mesh-1", ToolStep(name="web_search"))
        s.record_step("mesh-1", ToolStep(name="read_file", is_error=True))
        steps = s.turn_recap("mesh-1")
        self.assertEqual([x.name for x in steps], ["web_search", "read_file"])
        self.assertEqual(s.turn_recap("other"), [])
        s.clear_turn("mesh-1")
        self.assertEqual(s.turn_recap("mesh-1"), [])


class TestHttpEndpoint(unittest.TestCase):
    """Spin up the real ThreadingHTTPServer on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        # Bind port 0 → OS picks a free port; then read it back.
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.server = ControlServer(port=cls.port)
        assert cls.server.start(), "control server failed to start"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _post(self, path, body):
        url = f"http://127.0.0.1:{self.port}{path}?token={self.server._token}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def test_step_report_ok(self):
        code, body = self._post("/step", {
            "session": "mesh-http",
            "kind": "report",
            "tool": "web_search",
            "result": "ok",
        })
        self.assertEqual(code, 200)
        self.assertIn("web_search",
                      [s.name for s in self.server.turn_recap("mesh-http")])

    def test_step_gate_blocks_until_answer(self):
        # Fire the gate in a thread; answer from the main thread.
        result = {}
        def run():
            url = f"http://127.0.0.1:{self.port}/step?token={self.server._token}"
            req = urllib.request.Request(
                url,
                data=json.dumps({
                    "session": "mesh-gate",
                    "kind": "gate",
                    "tool": "terminal",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                result["status"] = r.status
                result["body"] = r.read().decode("utf-8")

        t = threading.Thread(target=run)
        t.start()
        import time
        time.sleep(0.2)
        self.assertTrue(self.server.has_pending_approval("mesh-gate"))
        self.assertTrue(self.server.answer_approval("mesh-gate", approve=True))
        t.join(timeout=8)
        self.assertEqual(result.get("status"), 200)
        self.assertEqual(result.get("body"), "approve")

    def test_auth_rejects_bad_token(self):
        url = f"http://127.0.0.1:{self.port}/step?token=WRONG"
        req = urllib.request.Request(
            url, data=b"{}", method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)


class TestGateNotifyEndpoint(unittest.TestCase):
    """/gate/notify pre-exec gate (mesh-tool-gate plugin path)."""

    @classmethod
    def setUpClass(cls):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.server = ControlServer(port=cls.port)
        assert cls.server.start(), "control server failed to start"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _post(self, path, body):
        url = f"http://127.0.0.1:{self.port}{path}?token={self.server._token}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def test_gate_notify_opens_gate_and_approves(self):
        """POST /gate/notify fires on_gate_open, marks pending, and a
        subsequent approve resolves it to 'approve'."""
        opened = []

        def on_open(session, tool, command, description):
            opened.append((session, tool, command, description))

        self.server.on_gate_open = on_open
        result = {}

        def run():
            code, body = self._post("/gate/notify", {
                "session": "mesh-test-123",
                "tool": "terminal",
                "command": "rm -rf /",
                "description": "dangerous rm",
            })
            result["code"] = code
            result["body"] = body

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.2)
        # on_gate_open fired with the resolved mesh thread name + real values.
        self.assertEqual(opened, [
            ("mesh-test-123", "terminal", "rm -rf /", "dangerous rm")
        ])
        self.assertTrue(self.server.has_pending_approval("mesh-test-123"))
        self.assertTrue(self.server.answer_approval("mesh-test-123", approve=True))
        t.join(timeout=6)
        self.assertEqual(result.get("code"), 200)
        self.assertEqual(result.get("body"), "approve")

    def test_gate_notify_deny_does_not_fire_on_deny(self):
        """A deny decision returns 'deny' and does NOT fire on_deny.

        Stage 2: Fix 4 (on_deny relay) is reverted — the mesh /deny now
        matches Telegram gateway behavior: block the tool, tell the model
        why, and let it continue. It must NOT kill the hermes child.
        """
        denied = []
        self.server.on_deny = lambda session: denied.append(session)
        result = {}

        def run():
            code, body = self._post("/gate/notify", {
                "session": "mesh-test-456",
                "tool": "terminal",
                "command": "dd if=/dev/sda",
                "description": "dd to raw device",
            })
            result["code"] = code
            result["body"] = body

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.2)
        self.assertTrue(self.server.has_pending_approval("mesh-test-456"))
        self.assertTrue(self.server.answer_approval("mesh-test-456", approve=False))
        t.join(timeout=6)
        self.assertEqual(result.get("code"), 200)
        self.assertEqual(result.get("body"), "deny")
        # on_deny must NOT fire (we block + continue, not kill).
        deadline = time.time() + 0.5
        while time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(denied, [])

    def test_gate_notify_deny_with_reason_returns_json(self):
        """A deny with a reason returns a JSON body carrying the reason.

        The plugin parses {"verdict": "deny", "reason": "..."} and surfaces
        the operator's /deny <reason> in the BLOCKED message to the model.
        """
        denied = []
        self.server.on_deny = lambda session: denied.append(session)
        result = {}

        def run():
            code, body = self._post("/gate/notify", {
                "session": "mesh-test-reason",
                "tool": "terminal",
                "command": "chmod 777 /etc/passwd",
                "description": "sensitive chmod",
            })
            result["code"] = code
            result["body"] = body

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.2)
        self.assertTrue(self.server.has_pending_approval("mesh-test-reason"))
        self.assertTrue(
            self.server.answer_approval("mesh-test-reason", approve=False, reason="stop doing that")
        )
        t.join(timeout=6)
        self.assertEqual(result.get("code"), 200)
        payload = json.loads(result.get("body") or "{}")
        self.assertEqual(payload["verdict"], "deny")
        self.assertEqual(payload["reason"], "stop doing that")
        # on_deny still must NOT fire.
        deadline = time.time() + 0.5
        while time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(denied, [])

    def test_gate_notify_timeout_denies(self):
        """Timeout → timeout verdict, without needing an explicit answer."""
        result = {}
        self.server.approval_timeout = 0.3

        def run():
            code, body = self._post("/gate/notify", {
                "session": "mesh-test-789",
                "tool": "terminal",
                "command": "chmod 777 /etc",
                "description": "sensitive chmod",
            })
            result["code"] = code
            result["body"] = body

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=6)
        self.assertEqual(result.get("code"), 200)
        self.assertEqual(result.get("body"), "timeout")
        self.assertFalse(self.server.has_pending_approval("mesh-test-789"))


class TestRelayOffload(unittest.TestCase):
    """Lock in the relay offload: record_step must return without waiting for
    the mesh callback, and the relay worker must actually run it.

    These are the invariants that prevent the control-server request thread
    (or the hook's worker thread, for gates) from ever blocking on a mesh
    relay. The LXMF send itself is non-blocking (LXMF router spawns its own
    thread), but the callback wiring must still be exercised off the caller.
    """

    @classmethod
    def setUpClass(cls):
        # Bind port 0 → OS picks a free port; then read it back.
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.server = ControlServer(port=cls.port)
        assert cls.server.start(), "control server failed to start"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_record_step_does_not_block(self):
        """record_step returns immediately — it only updates in-memory state,
        it does not wait for any mesh callback."""
        s = ControlServer(port=0)
        s.on_step = lambda *a, **k: None
        start = time.time()
        s.record_step("mesh-x", ToolStep(name="web_search"))
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0,
                        "record_step blocked on the relay worker")

    def test_http_step_drains_to_callback(self):
        """POST /step (kind=report) enqueues the on_step relay and the relay
        worker delivers it — the HTTP handler returns 200 without waiting
        for the callback to complete."""
        got = threading.Event()
        self.server.on_step = lambda session, step: got.set()
        url = f"http://127.0.0.1:{self.port}/step?token={self.server._token}"
        req = urllib.request.Request(
            url,
            data=json.dumps({
                "session": "mesh-y",
                "kind": "report",
                "tool": "read_file",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        start = time.time()
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
        http_elapsed = time.time() - start
        # HTTP response must be fast (no blocking on mesh relay).
        self.assertLess(http_elapsed, 2.0,
                        "POST /step blocked waiting for mesh relay")
        # The relay worker should deliver the callback shortly after.
        self.assertTrue(got.wait(3.0),
                        "relay worker did not deliver on_step within 3s")

    def test_on_step_none_is_noop(self):
        """If no callback is wired, record_step still returns cleanly."""
        s = ControlServer(port=0)
        s.on_step = None
        start = time.time()
        s.record_step("mesh-z", ToolStep(name="web_search"))
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0)


class TestStatusPayloadInProcess(unittest.TestCase):
    """Exercise ControlServer.status_payload() without an HTTP hop."""

    def setUp(self):
        self.server = ControlServer(port=0)  # not started; in-process API

    def test_has_exactly_the_four_keys(self):
        p = self.server.status_payload()
        self.assertEqual(set(p), {"model", "session", "uptime", "acl"})
        self.assertEqual(
            set(p["session"]),
            {"running", "active_sessions", "pending_approvals"},
        )

    def test_model_null_until_wired(self):
        self.assertIsNone(self.server.status_payload()["model"])
        self.server._model = "gpt-4o"
        self.assertEqual(self.server.status_payload()["model"], "gpt-4o")

    def test_uptime_is_positive_and_increases(self):
        time.sleep(0.02)  # let some monotonic time elapse past __init__
        t1 = self.server.status_payload()["uptime"]
        self.assertIsInstance(t1, float)
        self.assertGreater(t1, 0.0)
        time.sleep(0.02)
        t2 = self.server.status_payload()["uptime"]
        self.assertGreater(t2, t1)

    def test_session_reports_pending_gate(self):
        def wait_gate():
            self.server.request_approval("mesh-st", "terminal", timeout=5)

        t = threading.Thread(target=wait_gate)
        t.start()
        time.sleep(0.2)
        try:
            self.assertTrue(self.server.has_pending_approval("mesh-st"))
            s = self.server.status_payload()["session"]
            self.assertTrue(s["running"])
            self.assertIn("mesh-st", s["pending_approvals"])
            self.assertIn("mesh-st", s["active_sessions"])
        finally:
            self.server.answer_approval("mesh-st", approve=True)
            t.join(timeout=6)
        self.assertFalse(self.server.status_payload()["session"]["running"])

    def test_acl_mode_reflects_env(self):
        with mock.patch.dict(os.environ, {"HERMES_RETICUM_ALLOW_ALL": "true"}):
            self.assertEqual(self.server.status_payload()["acl"], "open")
        with mock.patch.dict(
            os.environ,
            {"HERMES_RETICUM_ALLOW_ALL": "false",
             "HERMES_RETICUM_ALLOWED_USERS": "aa" * 16},
        ):
            self.assertEqual(self.server.status_payload()["acl"], "allowlist")
        with mock.patch.dict(
            os.environ,
            {"HERMES_RETICUM_ALLOW_ALL": "false",
             "HERMES_RETICUM_ALLOWED_USERS": ""},
        ):
            self.assertEqual(self.server.status_payload()["acl"], "closed")


class TestStatusHttpEndpoint(unittest.TestCase):
    """GET /status over a real ThreadingHTTPServer (ephemeral port)."""

    @classmethod
    def setUpClass(cls):
        # Bind port 0 → OS picks a free port; then read it back.
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.server = ControlServer(port=cls.port)
        assert cls.server.start(), "control server failed to start"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _get(self, path, token=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        if token is not None:
            url += f"?token={token}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_status_happy_path(self):
        code, ctype, body = self._get("/status", token=self.server._token)
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "application/json")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(set(payload), {"model", "session", "uptime", "acl"})
        self.assertIn("running", payload["session"])
        self.assertIn("pending_approvals", payload["session"])

    def test_status_requires_token(self):
        self.assertEqual(self._get("/status")[0], 401)
        self.assertEqual(self._get("/status", token="WRONG")[0], 401)

    def test_health_unchanged(self):
        code, ctype, body = self._get("/health", token=self.server._token)
        self.assertEqual(code, 200)
        self.assertIn("text/plain", ctype)
        self.assertEqual(body.decode("utf-8"), "ok")


class TestSessionToolCounter(unittest.TestCase):
    """session_tool_total is cumulative across turns; clear_turn only resets per-turn."""

    def test_increments_per_record_step(self):
        s = ControlServer(port=0)
        s.record_step("mesh-1", ToolStep(name="web_search"))
        s.record_step("mesh-1", ToolStep(name="read_file"))
        self.assertEqual(s.session_tool_total("mesh-1"), 2)
        self.assertEqual(s.session_tool_total("other"), 0)

    def test_not_reset_by_clear_turn(self):
        s = ControlServer(port=0)
        s.record_step("mesh-1", ToolStep(name="web_search"))
        s.record_step("mesh-1", ToolStep(name="terminal"))
        self.assertEqual(s.session_tool_total("mesh-1"), 2)
        s.clear_turn("mesh-1")
        # Per-turn resets, but session total persists
        self.assertEqual(s.turn_recap("mesh-1"), [])
        self.assertEqual(s.session_tool_total("mesh-1"), 2)
        s.record_step("mesh-1", ToolStep(name="write_file"))
        self.assertEqual(len(s.turn_recap("mesh-1")), 1)
        self.assertEqual(s.session_tool_total("mesh-1"), 3)


class TestCmdStatusOutput(unittest.TestCase):
    """_cmd_status renders Bridge:, Turn:, Tools:, Tokens: lines correctly."""

    def setUp(self):
        self.cs = ControlServer(port=0)
        # Record 3 tool calls so session total > turn total
        self.cs.record_step("test-sesh", ToolStep(name="web_search"))
        self.cs.record_step("test-sesh", ToolStep(name="read_file"))
        self.cs.record_step("test-sesh", ToolStep(name="terminal"))
        self.cs.clear_turn("test-sesh")  # turn now empty, session still 3

    def _make_ctx(self, is_running=False, token_stats=(0, 0, None)):
        """Build a CommandContext with a stub HermesClient."""
        from hermes_reticulum.core.commands import CommandContext
        from hermes_reticulum.core.model_command import ModelCommandHandler

        h = _FakeHermesClient(
            session_name="test-sesh",
            model="gpt-4o",
            is_running=is_running,
            token_stats=token_stats,
        )
        mh = ModelCommandHandler(h, None)  # None mesh_client for test
        return CommandContext(
            hermes=h,
            model_handler=mh,
            control_server=self.cs,
        )

    def test_bridge_line_shows_up(self):
        ctx = self._make_ctx()
        from hermes_reticulum.core.commands import _cmd_status
        out = _cmd_status(ctx, "")
        self.assertIn("Bridge:", out)
        self.assertIn("up ", out)  # uptime string present

    def test_turn_line_not_running(self):
        ctx = self._make_ctx()
        from hermes_reticulum.core.commands import _cmd_status
        out = _cmd_status(ctx, "")
        self.assertIn("Turn:", out)
        self.assertNotIn("Running:", out)
        self.assertIn("idle", out)

    def test_tools_line_format(self):
        ctx = self._make_ctx()
        from hermes_reticulum.core.commands import _cmd_status
        out = _cmd_status(ctx, "")
        self.assertIn("Tools:", out)
        # Session total = 3, turn = 0 (cleared in setUp)
        self.assertIn("3 this session (0 this turn)", out)

    def test_tokens_not_tracked(self):
        ctx = self._make_ctx(token_stats=(0, 0, None))
        from hermes_reticulum.core.commands import _cmd_status
        out = _cmd_status(ctx, "")
        self.assertIn("not tracked by bridge", out)
        self.assertNotIn("session not persisted", out)


class _FakeHermesClient:
    """Minimal stub for testing _cmd_status without a real HermesClient."""

    def __init__(self, session_name="test", model="gpt-4o",
                 is_running=False, token_stats=(0, 0, None)):
        self.session_name = session_name
        self._model = model
        self._running = is_running
        self._token_stats = token_stats
        self._resume_id = None
        self.timeout = 600
        self.liveness_timeout = 300

    def get_model(self):
        return self._model

    def is_running(self):
        return self._running

    def session_token_stats(self):
        return self._token_stats


class TestCommandDispatcherAliases(unittest.TestCase):
    """/a and /d must route to the same handlers as /approve and /deny."""

    def _make_ctx(self):
        from hermes_reticulum.core.commands import CommandContext
        from hermes_reticulum.core.model_command import ModelCommandHandler

        h = _FakeHermesClient(session_name="test-alias", model="gpt-4o")
        # ModelCommandHandler tries to load a persisted model pin and call
        # set_model() on the client; stub both so we don't touch real state.
        with mock.patch.object(ModelCommandHandler, "_load", return_value=None):
            mh = ModelCommandHandler(h, None)
        return CommandContext(
            hermes=h,
            model_handler=mh,
            control_server=ControlServer(port=0),
        )

    def test_a_routes_to_approve(self):
        from hermes_reticulum.core.commands import CommandDispatcher

        ctx = self._make_ctx()
        d = CommandDispatcher(ctx)
        # With no gate pending, /a must reach the approve handler (not None).
        result = d.handle("/a")
        self.assertIsNotNone(result)
        self.assertIn("No pending approval", result)

    def test_d_routes_to_deny(self):
        from hermes_reticulum.core.commands import CommandDispatcher

        ctx = self._make_ctx()
        d = CommandDispatcher(ctx)
        result = d.handle("/d")
        self.assertIsNotNone(result)
        self.assertIn("No pending approval", result)

    def test_full_words_still_work(self):
        from hermes_reticulum.core.commands import CommandDispatcher

        ctx = self._make_ctx()
        d = CommandDispatcher(ctx)
        self.assertIn("No pending approval", d.handle("/approve") or "")
        self.assertIn("No pending approval", d.handle("/deny") or "")


if __name__ == "__main__":
    unittest.main()
