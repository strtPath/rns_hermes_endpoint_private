"""
Tests for Hermes for Reticulum.
"""

import hermes_reticulum.core.hermes_client as _hc
from hermes_reticulum.core.acl import AccessControl
from hermes_reticulum.core.hermes_client import HermesClient, find_hermes_bin


def _mock_popen(monkeypatch, returncode, stdout, stderr):
    """Patch the *real* subprocess seam used by _run_with_liveness_guard.

    The guard spawns the hermes child via ``subprocess.Popen`` (not
    ``subprocess.run``), so mocking ``subprocess.run`` has no effect and the
    real Popen tries to exec the (nonexistent) binary → FileNotFoundError.
    """
    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = returncode
            self.stdout = iter(stdout.splitlines()) if stdout else iter(())
            self.stderr = iter(stderr.splitlines()) if stderr else iter(())

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(_hc.subprocess, "Popen", lambda cmd, **kw: FakePopen(cmd, **kw))


class TestACL:
    """Test the access control module."""

    def test_open_mode(self, monkeypatch):
        monkeypatch.setenv("HERMES_RETICUM_ALLOW_ALL", "true")
        monkeypatch.delenv("HERMES_RETICUM_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("HERMES_RETICUM_BLOCKED_USERS", raising=False)

        acl = AccessControl()
        assert acl.mode == "open"
        assert acl.is_allowed("aabbccdd" * 4) is True

    def test_allowlist_mode(self, monkeypatch):
        monkeypatch.setenv("HERMES_RETICUM_ALLOW_ALL", "false")
        monkeypatch.setenv("HERMES_RETICUM_ALLOWED_USERS", "aabbccdd" * 4)
        monkeypatch.delenv("HERMES_RETICUM_BLOCKED_USERS", raising=False)

        acl = AccessControl()
        assert acl.mode == "allowlist"
        assert acl.is_allowed("aabbccdd" * 4) is True
        assert acl.is_allowed("11223344" * 4) is False

    def test_blocklist(self, monkeypatch):
        monkeypatch.setenv("HERMES_RETICUM_ALLOW_ALL", "true")
        monkeypatch.setenv("HERMES_RETICUM_BLOCKED_USERS", "deadbeef" * 4)
        monkeypatch.delenv("HERMES_RETICUM_ALLOWED_USERS", raising=False)

        acl = AccessControl()
        assert acl.is_allowed("deadbeef" * 4) is False
        assert acl.is_allowed("aabbccdd" * 4) is True

    def test_invalid_hash_ignored(self, monkeypatch):
        monkeypatch.setenv("HERMES_RETICUM_ALLOW_ALL", "false")
        # short=bad, toolong=bad, valid=good
        valid = "1122334411223344aabbccdd11223344"
        monkeypatch.setenv("HERMES_RETICUM_ALLOWED_USERS", f"short,toolonghashvalue,{valid}")
        monkeypatch.delenv("HERMES_RETICUM_BLOCKED_USERS", raising=False)

        acl = AccessControl()
        # Only the valid 32-char hash should be in the set
        assert len(acl.allowed_users) == 1


class TestFindHermesBin:
    """Test hermes binary detection."""

    def test_find_in_path(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/hermes" if x == "hermes" else None)
        assert find_hermes_bin() == "/usr/bin/hermes"

    def test_find_known_location(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda x: None)
        # Create a fake hermes binary
        fake = tmp_path / "hermes"
        fake.write_text("#!/bin/sh\necho ok")
        fake.chmod(0o755)

        # Patch the candidates list
        import hermes_reticulum.core.hermes_client as hc
        original = hc._HERMES_CANDIDATES
        hc._HERMES_CANDIDATES = [str(fake)]
        try:
            assert find_hermes_bin() == str(fake)
        finally:
            hc._HERMES_CANDIDATES = original

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: None)
        import hermes_reticulum.core.hermes_client as hc
        original = hc._HERMES_CANDIDATES
        hc._HERMES_CANDIDATES = []
        try:
            assert find_hermes_bin() is None
        finally:
            hc._HERMES_CANDIDATES = original


class TestHermesClient:
    """Test the Hermes client (mocked)."""

    def test_chat_returns_reply(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_reticulum.core.hermes_client.find_hermes_bin",
            lambda: "/usr/bin/hermes",
        )
        # Isolate from the real ~/.hermes/state.db: with no DB the session
        # can't resolve, so no tool-recap suffix is appended to the reply.
        monkeypatch.setenv("HERMES_STATE_DB", "/nonexistent/state.db")
        _mock_popen(monkeypatch, 0, "Hello! How can I help you?", "")
        client = HermesClient()
        reply = client.chat("Hello")
        assert reply == "Hello! How can I help you?"

    def test_chat_handles_timeout(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_reticulum.core.hermes_client.find_hermes_bin",
            lambda: "/usr/bin/hermes",
        )
        client = HermesClient(timeout=300)
        # A real timeout is surfaced by the liveness guard as a "Turn exceeded
        # the liveness window" message, so stub the guard (the real seam) and
        # assert on that wording.
        client._run_with_liveness_guard = lambda cmd: (
            "⏱️ Turn exceeded the liveness window (300s) — the model may "
            "be busy or stalled. Please retry."
        )
        reply = client.chat("Hello")
        assert "timeout" in reply.lower() or "exceeded" in reply.lower()

    def test_chat_handles_file_not_found(self, monkeypatch):
        def mock_run(cmd, **kwargs):
            raise FileNotFoundError("hermes not found")

        monkeypatch.setattr("subprocess.run", mock_run)
        monkeypatch.setattr(
            "hermes_reticulum.core.hermes_client.find_hermes_bin",
            lambda: "/nonexistent/hermes",
        )

        client = HermesClient()
        reply = client.chat("Hello")
        assert "not found" in reply.lower()

    def test_chat_handles_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_reticulum.core.hermes_client.find_hermes_bin",
            lambda: "/usr/bin/hermes",
        )
        _mock_popen(monkeypatch, 1, "", "Error: something broke")
        client = HermesClient()
        reply = client.chat("Hello")
        assert "error" in reply.lower()


class TestVersion:
    """Test package metadata."""

    def test_version_importable(self):
        from hermes_reticulum import __version__
        assert __version__ == "0.1.0"


class TestBridgeLiveness:
    """Test the Tier 4.5 bridge liveness proxy (Option 3b)."""

    def _lv(self, tmp_path, healthy=True, interval=60.0):
        from hermes_reticulum.core import bridge_liveness as bl

        sent = []
        lv = bl.BridgeLiveness(
            heartbeat_file=str(tmp_path / "hb"),
            interval=interval,
            rns_probe=lambda: healthy,
            notify=lambda msg: sent.append(msg) or True,
        )
        return lv, sent

    def test_start_pings_ready_and_watchdog(self, tmp_path):
        import os
        lv, sent = self._lv(tmp_path, healthy=True)
        lv.start()
        # READY=1 on start, WATCHDOG=1 on the first (unconditional) tick.
        assert sent == ["READY=1", "WATCHDOG=1"]
        assert os.path.exists(str(tmp_path / "hb"))
        lv.stop()

    def test_tick_healthy_writes_marker_and_pings(self, tmp_path):
        import os
        import json
        lv, sent = self._lv(tmp_path, healthy=True)
        lv.start()
        sent.clear()
        lv._tick(healthy=True)
        assert sent == ["WATCHDOG=1"]
        assert os.path.exists(str(tmp_path / "hb"))
        data = json.loads((tmp_path / "hb").read_text())
        assert "ts" in data and "pid" in data
        lv.stop()

    def test_tick_unhealthy_stops_pinging(self, tmp_path):
        import os
        lv, sent = self._lv(tmp_path, healthy=True)
        lv.start()
        # Remove marker to prove a bad tick does NOT re-create it.
        os.unlink(str(tmp_path / "hb"))
        sent.clear()
        lv._tick(healthy=False)
        assert sent == []  # no WATCHDOG ping
        assert not os.path.exists(str(tmp_path / "hb"))  # no marker write
        lv.stop()

    def test_snapshot_reports_state(self, tmp_path):
        lv, _ = self._lv(tmp_path, healthy=True)
        lv.start()
        snap = lv.snapshot()
        assert snap["rns_healthy"] is True
        assert snap["last_tick_age_s"] is not None
        assert snap["last_tick_age_s"] < 5
        assert snap["marker_age_s"] is not None
        lv.stop()

    def test_sd_notify_no_socket_returns_false(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        from hermes_reticulum.core.bridge_liveness import sd_notify
        assert sd_notify("WATCHDOG=1") is False

    def test_sd_notify_sends_to_socket(self, tmp_path, monkeypatch):
        import socket
        monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "notify.sock"))
        recv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        recv.bind(str(tmp_path / "notify.sock"))
        try:
            from hermes_reticulum.core.bridge_liveness import sd_notify
            assert sd_notify("WATCHDOG=1") is True
            data, _ = recv.recvfrom(64)
            assert data == b"WATCHDOG=1"
        finally:
            recv.close()

    def test_default_probe_fresh(self, monkeypatch):
        import RNS
        import time
        monkeypatch.setattr(RNS.Transport, "interface_last_jobs", time.time())
        from hermes_reticulum.core.bridge_liveness import default_rns_probe
        assert default_rns_probe() is True

    def test_default_probe_stale(self, monkeypatch):
        import RNS
        import time
        monkeypatch.setattr(RNS.Transport, "interface_last_jobs", time.time() - 9999)
        from hermes_reticulum.core.bridge_liveness import default_rns_probe
        assert default_rns_probe() is False

    def test_default_probe_never_set(self, monkeypatch):
        import RNS
        monkeypatch.setattr(RNS.Transport, "interface_last_jobs", 0.0)
        from hermes_reticulum.core.bridge_liveness import default_rns_probe
        assert default_rns_probe() is False

    def test_stop_is_idempotent(self, tmp_path):
        lv, _ = self._lv(tmp_path, healthy=True)
        lv.start()
        lv.stop()
        lv.stop()  # must not raise or hang


class TestPreflight:
    """Test the preflight check module."""

    def test_ok_when_binary_exists(self, monkeypatch, tmp_path):
        from hermes_reticulum.core.preflight import run_preflight

        fake_bin = tmp_path / "hermes"
        fake_bin.write_text("#!/bin/sh\necho hermes version 0.1.0")
        fake_bin.chmod(0o755)

        monkeypatch.setenv("RETICULUM_STORAGE", str(tmp_path / "storage"))
        monkeypatch.setenv("HERMES_CONFIG", str(tmp_path / "config.yaml"))

        result = run_preflight(
            hermes_bin=str(fake_bin),
            storage=str(tmp_path / "storage"),
            config_yaml=str(tmp_path / "config.yaml"),
            check_plugins=False,
        )
        # Binary found and ran
        assert result.checks, "should have at least one check"
        # No hard errors (storage dir doesn't exist → warning, not error)
        for e in result.errors:
            assert "binary" not in e.lower()

    def test_error_when_binary_missing(self, monkeypatch, tmp_path):
        from hermes_reticulum.core.preflight import run_preflight

        result = run_preflight(
            hermes_bin=str(tmp_path / "nonexistent"),
            storage=str(tmp_path / "storage"),
            config_yaml=str(tmp_path / "config.yaml"),
            check_plugins=False,
        )
        assert result.ok is False
        assert any("does not exist" in e for e in result.errors)

    def test_render_includes_errors(self, monkeypatch, tmp_path):
        from hermes_reticulum.core.preflight import PreflightResult

        result = PreflightResult(ok=False)
        result.errors.append("Test error one")
        result.warnings.append("Test warning one")
        rendered = result.render()
        assert "Test error one" in rendered
        assert "Test warning one" in rendered
        assert "cannot start" in rendered

    def test_render_ok_only_checks(self):
        from hermes_reticulum.core.preflight import PreflightResult

        result = PreflightResult(ok=True)
        result.checks.append("Hermes binary: /usr/bin/hermes")
        rendered = result.render()
        assert "Hermes binary" in rendered
        assert "cannot start" not in rendered

    def test_storage_warning_when_missing(self, monkeypatch, tmp_path):
        from hermes_reticulum.core.preflight import run_preflight

        result = run_preflight(
            hermes_bin="/usr/bin/hermes",
            storage=str(tmp_path / "does_not_exist"),
            config_yaml=str(tmp_path / "config.yaml"),
            check_plugins=False,
        )
        assert any("does_not_exist" in w for w in result.warnings)

    def test_config_warning_when_missing(self, monkeypatch, tmp_path):
        from hermes_reticulum.core.preflight import run_preflight

        result = run_preflight(
            hermes_bin="/usr/bin/hermes",
            storage=str(tmp_path),
            config_yaml=str(tmp_path / "no_such_config.yaml"),
            check_plugins=False,
        )
        assert any("config not found" in w for w in result.warnings)

    def test_plugin_warning_when_missing(self, monkeypatch, tmp_path):
        from hermes_reticulum.core.preflight import run_preflight

        # Point HOME at tmp_path so ~/.hermes/plugins doesn't exist
        monkeypatch.setenv("HOME", str(tmp_path))
        result = run_preflight(
            hermes_bin="/usr/bin/hermes",
            storage=str(tmp_path / "storage"),
            config_yaml=str(tmp_path / "config.yaml"),
            check_plugins=True,
        )
        assert any("mesh-tool-gate" in w for w in result.warnings)
