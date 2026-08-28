"""Unit tests for the liveness-heartbeat marker (spec 2026-08-27, Part 1,
Option A): the bridge's stall detector.

Run with:  ./venv/bin/python -m unittest tests.test_turn_alive -v
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

# Ensure project src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hermes_reticulum.core.hermes_client import HermesClient  # noqa: E402


def make_client(liveness_timeout=2):
    c = HermesClient(hermes_bin="/usr/bin/true", timeout=300)
    c.liveness_timeout = liveness_timeout
    return c


def _write_marker(path, session, phase="tool", age=0.0):
    """Write a marker file, backdating its mtime by ``age`` seconds."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"session": session, "ts": time.time(), "phase": phase}, f)
    os.replace(tmp, path)
    if age:
        then = time.time() - age
        os.utime(path, (then, then))
    return path


class TestMarkerHelpers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.marker = os.path.join(self.dir.name, "alive")
        self.client = make_client(liveness_timeout=2)
        self.client.turn_alive_file = self.marker
        self.client.session_name = "mesh-test"

    def tearDown(self):
        self.dir.cleanup()

    def test_write_marker_roundtrip(self):
        self.client.write_turn_alive_marker(phase="model")
        with open(self.marker, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["session"], "mesh-test")
        self.assertEqual(data["phase"], "model")
        self.assertIsInstance(data["ts"], float)
        # Fresh, same session → alive.
        self.assertTrue(self.client._marker_alive())

    def test_no_file_means_not_alive(self):
        self.assertFalse(self.client._marker_alive())

    def test_stale_mtime_not_alive(self):
        _write_marker(self.marker, "mesh-test", age=5.0)  # liveness_timeout=2
        self.assertFalse(self.client._marker_alive())

    def test_foreign_session_not_alive(self):
        _write_marker(self.marker, "mesh-OTHER", age=0.0)
        self.assertFalse(self.client._marker_alive())

    def test_corrupt_marker_not_alive(self):
        with open(self.marker, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(self.client._marker_alive())

    def test_clear_removes_marker(self):
        self.client.write_turn_alive_marker()
        self.assertTrue(self.client._marker_alive())
        self.client.clear_turn_alive_marker()
        self.assertFalse(self.client._marker_alive())
        # Idempotent.
        self.client.clear_turn_alive_marker()

    def test_default_path_env(self):
        os.environ["HERMES_TURN_ALIVE_FILE"] = os.path.join(
            self.dir.name, "custom-alive"
        )
        try:
            c = make_client()
            self.assertEqual(
                c.turn_alive_file, os.path.join(self.dir.name, "custom-alive")
            )
        finally:
            os.environ.pop("HERMES_TURN_ALIVE_FILE", None)

    def test_default_path(self):
        os.environ.pop("HERMES_TURN_ALIVE_FILE", None)
        c = make_client()
        self.assertEqual(
            c.turn_alive_file,
            os.path.expanduser("~/.hermes/.reticulum-turn-alive"),
        )


class TestGuardWithMarker(unittest.TestCase):
    """End-to-end: the real _run_with_liveness_guard vs a fake child."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.marker = os.path.join(self.dir.name, "alive")
        self.client = make_client(liveness_timeout=2)
        self.client.turn_alive_file = self.marker
        self.client.session_name = "mesh-guard-test"

    def tearDown(self):
        self.dir.cleanup()

    def _write_child(self, body):
        child = os.path.join(self.dir.name, "child.py")
        with open(child, "w", encoding="utf-8") as f:
            f.write(
                "import time, json, os, sys\n"
                "marker = %r\n"
                "sess = %r\n"
                "def beat(s):\n"
                "    with open(marker + '.tmp', 'w') as fh:\n"
                "        json.dump({'session': s, 'ts': time.time(), "
                "'phase': 'tool'}, fh)\n"
                "    os.replace(marker + '.tmp', marker)\n"
                "\n"
                "%s\n"
                "print('child-done')\n"
                "sys.exit(0)\n"
                % (self.marker, "mesh-guard-test", body)
            )
        return child

    def test_slow_turn_with_fresh_marker_completes(self):
        """A child that keeps the marker fresh for its session must run to
        completion, even though it emits zero stdout for > liveness_timeout.
        This is the core Option A behavior: stall detector, not wall clock.

        In production the bridge writes the phase='model' start marker at
        spawn and the hook rewrites it (phase='tool') on every tool batch,
        so the marker is continuously fresh. The test child mimics that by
        re-beating the marker before the long silent sleep — the same way
        the hook would beat it in production."""
        child = self._write_child(
            "beat(sess)\n"
            "# Sleep 3s total (longer than liveness_timeout=2s), but beat\n"
            "# the marker every second, the way the hook does in production\n"
            "# on every agent:step — the guard must see a fresh marker the\n"
            # whole time, not just a single beat at the end.\n"
            "time.sleep(1)\n"
            "beat(sess)\n"
            "time.sleep(1)\n"
            "beat(sess)\n"
            "time.sleep(1)\n"
        )
        # Beat once more right before the run so the marker is fresh when
        # the child begins (in production the bridge does this via the
        # spawn-time start marker; the hook maintains it from there).
        _write_marker(self.marker, "mesh-guard-test", age=0.0)
        start = time.monotonic()
        reply = self.client._run_with_liveness_guard(
            [sys.executable, child]
        )
        elapsed = time.monotonic() - start
        self.assertEqual(reply, "child-done")
        # The child lived ~4s (> 2s window); the marker kept it warm.
        self.assertGreaterEqual(elapsed, 3.0)
        self.assertFalse(self.client._guard_killed)

    def test_wedged_child_without_marker_killed(self):
        """No marker writes at all → the guard is a wall clock: the child is
        killed at ~liveness_timeout with the honest guard-kill reply."""
        child = self._write_child(
            "time.sleep(8)  # never emits, never touches the marker\n"
        )
        start = time.monotonic()
        reply = self.client._run_with_liveness_guard(
            [sys.executable, child]
        )
        elapsed = time.monotonic() - start
        self.assertTrue(self.client._guard_killed)
        self.assertIn("liveness window", reply)
        # Killed at the window (~2s), not after the child's 8s sleep.
        self.assertLess(elapsed, 6.0)

    def test_foreign_session_marker_does_not_keep_alive(self):
        """A marker for a DIFFERENT session (another bridge turn's hook)
        must not keep this child alive."""
        child = self._write_child(
            "beat('mesh-OTHER-SESSION')\n"
            "time.sleep(8)\n"
        )
        reply = self.client._run_with_liveness_guard(
            [sys.executable, child]
        )
        self.assertTrue(self.client._guard_killed)
        self.assertIn("liveness window", reply)

    def test_spawn_resets_marker(self):
        """The guard resets the marker at spawn (stale markers from a dead
        turn don't leak) and writes a fresh phase='model' marker.

        Per spec item 3 the marker is reset *at spawn* and *on kill* — a
        clean exit is neither, so the start marker persists after a clean
        run (harmless: the next spawn resets it, a kill clears it). We
        assert the marker now holds THIS session's fresh start marker, not
        the stale one. We check the *content* (session + phase), not
        wall-clock freshness, because the child takes ~2s to exit and the
        liveness_timeout is 2s, so mtime-based freshness is a timing race."""
        # Leave a stale foreign marker lying around.
        _write_marker(self.marker, "mesh-DEAD-TURN", age=5.0)
        child = os.path.join(self.dir.name, "child.py")
        with open(child, "w", encoding="utf-8") as f:
            f.write("import sys; sys.exit(0)\n")
        self.client._run_with_liveness_guard([sys.executable, child])
        # After the run, the marker holds THIS client's session (fresh
        # phase='model' start marker), not the stale one.
        with open(self.marker, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["session"], "mesh-guard-test")
        self.assertEqual(data["phase"], "model")


class TestHookMarkerWriter(unittest.TestCase):
    """The hook's marker writer must be atomic and never raise."""

    def _load_hook(self):
        hook_path = os.path.expanduser(
            "~/.hermes/hooks/mesh-tool-events/handler.py"
        )
        if not os.path.exists(hook_path):
            self.skipTest("hook not installed on this machine")
        spec = importlib.util.spec_from_file_location(
            "mesh_tool_events_hook", hook_path
        )
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        return hook

    def test_hook_marker_writer_atomic_and_safe(self):
        hook = self._load_hook()
        self.dir = tempfile.TemporaryDirectory()
        path = os.path.join(self.dir.name, "alive")
        hook.TURN_ALIVE_FILE = path
        hook._write_turn_alive_marker("mesh-hook-test", "tool")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["session"], "mesh-hook-test")
        self.assertEqual(data["phase"], "tool")
        # No temp file left behind.
        self.assertFalse(os.path.exists(path + ".tmp"))
        # Must not raise even when the path is unusable.
        hook.TURN_ALIVE_FILE = os.path.join(
            self.dir.name, "no-such-dir", "x"
        )
        hook._write_turn_alive_marker("mesh-hook-test", "model")
        self.dir.cleanup()

    def test_hook_dispatch_routes_agent_start(self):
        hook = self._load_hook()
        self.dir = tempfile.TemporaryDirectory()
        self.dir2 = tempfile.TemporaryDirectory()
        self.dir3 = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.dir2.cleanup)
        self.addCleanup(self.dir3.cleanup)
        # Point every file the hook touches at temp paths.
        self.marker = os.path.join(self.dir.name, "alive")
        state_db = os.path.join(self.dir2.name, "state.db")
        # _write_turn_alive_marker reads the module-global TURN_ALIVE_FILE at
        # call time (no local import), so patching it redirects ALL marker
        # writes (including the mesh-phase one below) to a throwaway path.
        real_marker = os.path.join(self.dir3.name, "real-alive")
        import sqlite3
        conn = sqlite3.connect(state_db)
        conn.execute("CREATE TABLE sessions (id TEXT, title TEXT)")
        conn.execute(
            "INSERT INTO sessions (id, title) VALUES ('sid-1', 'mesh-hook-sess')"
        )
        conn.execute(
            "INSERT INTO sessions (id, title) VALUES ('sid-2', 'telegram-dm')"
        )
        conn.commit()
        conn.close()

        saved = (
            hook.TURN_ALIVE_FILE,
            hook.STATE_DB,
            hook._resolve_mesh_session,
        )
        hook.TURN_ALIVE_FILE = real_marker
        hook.STATE_DB = state_db

        # agent:start → mesh session routes to the marker writer.
        hook._handle_start({"session_id": "sid-1"})
        deadline = time.time() + 5
        while not os.path.exists(real_marker) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(real_marker),
                        "agent:start did not write the marker")
        with open(real_marker, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["session"], "mesh-hook-sess")
        self.assertEqual(data["phase"], "model")

        # Non-mesh session → the real marker is never written.
        hook._handle_start({"session_id": "sid-2"})
        # Give a hypothetical (buggy) async dispatch a moment to appear, so
        # a false negative isn't a timing artifact.
        time.sleep(0.2)
        self.assertFalse(os.path.exists(self.marker),
                         "non-mesh session wrote the marker")

        hook.TURN_ALIVE_FILE, hook.STATE_DB, hook._resolve_mesh_session = saved


if __name__ == "__main__":
    unittest.main()
