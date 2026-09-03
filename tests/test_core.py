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
