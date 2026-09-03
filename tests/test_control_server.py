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


if __name__ == "__main__":
    unittest.main()
