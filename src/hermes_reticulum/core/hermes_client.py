"""
Hermes Integration — calls Hermes Agent to process messages and return replies.

Supports two modes:
  1. CLI mode (default): calls `hermes chat -q` as a subprocess
  2. Future: direct Python import when Hermes is importable
"""

import logging
import os
import shutil
import subprocess
import threading
import time

logger = logging.getLogger("hermes_reticulum.hermes")

# Common locations for the hermes binary
_HERMES_CANDIDATES = [
    "hermes",  # in PATH
    "/opt/hermes/.venv/bin/hermes",
    "/opt/hermes/bin/hermes",
    os.path.expanduser("~/.hermes/bin/hermes"),
    os.path.expanduser("~/.local/bin/hermes"),
]


def find_hermes_bin() -> str | None:
    """
    Auto-detect the hermes binary path.
    Checks PATH first, then known installation locations.
    """
    # Check PATH first
    found = shutil.which("hermes")
    if found:
        return found

    # Check known locations
    for candidate in _HERMES_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


class HermesClient:
    """
    Client that sends messages to Hermes Agent and returns replies.

    Handles the subprocess call, timeout, output parsing, and error recovery.
    """

    def __init__(
        self,
        hermes_bin: str | None = None,
        timeout: int = 300,
        source_tag: str = "reticulum",
        extra_args: list[str] | None = None,
        model: str | None = None,
    ):
        """
        Args:
            hermes_bin: Path to the hermes CLI binary. Auto-detected if None.
            timeout: Max seconds to wait for a reply.
            source_tag: Source tag passed to --source for session tracking.
            extra_args: Additional args to pass to hermes chat.
            model: Model name to pin (passed as `-m <model>`). If None, uses
                `HERMES_MODEL` env var; if that's unset too, the hermes default.
        """
        if hermes_bin is None:
            hermes_bin = find_hermes_bin()
        if hermes_bin is None:
            raise RuntimeError(
                "Hermes binary not found. Install Hermes or set HERMES_BIN env var."
            )

        self.hermes_bin = hermes_bin
        self.timeout = timeout
        # Liveness guard: if zero output (stdout+stderr) for this many seconds,
        # the model is considered wedged. Set to 0 to disable.
        self.liveness_timeout = int(
            os.getenv("HERMES_LIVENESS_TIMEOUT", "180")
        )
        self.source_tag = source_tag
        self.extra_args = extra_args or []
        self.model = model or os.getenv("HERMES_MODEL", "").strip() or None
        self.session_name = os.getenv(
            "HERMES_SESSION_NAME", f"mesh-{source_tag}"
        )
        self._model_lock = __import__("threading").Lock()
        self._resume_id: str | None = None
        self._process: subprocess.Popen | None = None
        self._stop_requested = False

        logger.info("Hermes binary: %s", self.hermes_bin)
        if self.model:
            logger.info("Hermes model pinned: %s", self.model)
        logger.info("Hermes session thread: %s", self.session_name)
        if self.liveness_timeout:
            logger.info("Liveness guard: %ds (0=off)", self.liveness_timeout)

    def _resolve_session_id(self) -> str | None:
        """
        Resolve the named mesh thread to a concrete session ID.

        Queries ``~/.hermes/state.db`` directly (read-only) instead of
        shelling out to ``hermes sessions export``. This is faster and
        avoids the source-mismatch bug: sessions created via
        ``-c <name> --create-if-missing`` are stored with source='cli',
        not the bridge's source_tag, so an export filtered on
        ``--source reticulum`` never finds them.

        Returns the most recently active session whose title matches
        exactly, or None if no such session exists.
        """
        import os
        import sqlite3

        db_path = os.path.expanduser(
            os.environ.get("HERMES_STATE_DB", "~/.hermes/state.db")
        )
        if not os.path.exists(db_path):
            logger.debug("state.db not found at %s", db_path)
            return None

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT id FROM sessions "
                    "WHERE title = ? "
                    "ORDER BY last_activity_at DESC LIMIT 1",
                    (self.session_name,),
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                logger.debug(
                    "Resolved mesh thread %r -> session %s",
                    self.session_name, row[0],
                )
                return row[0]
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Could not query state.db for %r: %s",
                           self.session_name, exc)
        return None

    def _ensure_session(self, new_session: bool) -> None:
        """
        Maintain the resume target.

        - new_session=True  → start a fresh thread (bump name, clear cache).
        - new_session=False → resolve the named thread to an ID; create the
          titled session once if it doesn't exist yet.

        Sets self._resume_id (the concrete session ID to resume) or None.
        """
        if new_session:
            import time

            self.session_name = f"mesh-{self.source_tag}-{int(time.time())}"
            self._resume_id = None
            logger.info("Session reset — new thread: %s", self.session_name)
            return

        if self._resume_id:
            return  # already pinned for this thread
        resolved = self._resolve_session_id()
        if resolved:
            self._resume_id = resolved
            logger.info("Resuming mesh thread %r via session %s",
                        self.session_name, resolved[:12])
            return
        # No resolvable thread yet: leave _resume_id None. The first `chat()`
        # call falls back to `-c <name> --create-if-missing`, which opens the
        # titled session; the next call resolves it to an ID and pins it.
        logger.info("Mesh thread %r not resolvable yet — will create on send",
                    self.session_name)

    def chat(self, message: str, new_session: bool = False) -> str | None:
        """
        Send a message to Hermes and return the text reply.

        Args:
            message: The user's message text.
            new_session: If True, this call starts a fresh conversation
                thread (drops prior context). See _ensure_session.

        Returns:
            The agent's reply text, or None on error.
        """
        self._ensure_session(new_session)
        self._stop_requested = False

        cmd = [
            self.hermes_bin,
            "chat",
            "-q",
            message,
            "--source",
            self.source_tag,
            "-Q",  # quiet — suppress banner/spinner
        ]
        # Session handling:
        #  - resume by explicit session ID (deterministic), or
        #  - create/resume the named thread if we couldn't pin an ID yet.
        if self._resume_id:
            cmd += ["--resume", self._resume_id]
        else:
            cmd += ["-c", self.session_name, "--create-if-missing"]
        with self._model_lock:
            model = self.model
        if model:
            cmd += ["-m", model]
        cmd.extend(self.extra_args)

        try:
            logger.debug("Calling: %s", " ".join(cmd))
            return self._run_with_liveness_guard(cmd)

        except FileNotFoundError:
            logger.error("Hermes binary not found at: %s", self.hermes_bin)
            return "❌ Hermes Agent not found. Check your installation."

        except Exception as e:
            logger.error("Unexpected error calling Hermes: %s", e, exc_info=True)
            return f"❌ Unexpected error: {str(e)[:200]}"

    def _run_with_liveness_guard(self, cmd: list[str]) -> str | None:
        """
        Run the hermes subprocess with a liveness guard.

        Kills the process if zero output (stdout+stderr) for
        ``liveness_timeout`` seconds. Returns the reply text or an
        error message.
        """
        last_activity = time.monotonic()
        activity_lock = threading.Lock()
        done = threading.Event()

        def _touch():
            nonlocal last_activity
            with activity_lock:
                last_activity = time.monotonic()

        def _read_stream(stream, parts: list[str]):
            """Read a stream line-by-line until EOF, touching the liveness clock."""
            for line in stream:
                parts.append(line)
                _touch()
            stream.close()

        def _watchdog():
            if not self.liveness_timeout:
                return
            while not done.is_set():
                with activity_lock:
                    remaining = (last_activity + self.liveness_timeout) - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Liveness guard: no output for %ds, killing hermes",
                        self.liveness_timeout,
                    )
                    self._kill_process()
                    return
                done.wait(min(remaining, 1.0))

        proc: subprocess.Popen | None = None
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            self._process = proc

            stdout_thread = threading.Thread(
                target=_read_stream, args=(proc.stdout, stdout_parts),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_read_stream, args=(proc.stderr, stderr_parts),
                daemon=True,
            )
            watcher = threading.Thread(
                target=_watchdog, daemon=True, name="hermes-liveness"
            )

            stdout_thread.start()
            stderr_thread.start()
            watcher.start()

            proc.wait()

            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            watcher.join(timeout=2)

            stdout = "".join(stdout_parts)
            stderr = "".join(stderr_parts)

            if proc.returncode != 0:
                logger.error(
                    "Hermes exited with code %d: %s",
                    proc.returncode,
                    stderr[:500] if stderr else "(no stderr)",
                )
                return self._error_reply(proc.returncode, stderr)

            reply = stdout.strip()
            if not reply:
                logger.warning("Hermes returned empty output")
                return "_(no response)_"
            return reply

        except FileNotFoundError:
            logger.error("Hermes binary not found at: %s", self.hermes_bin)
            return "❌ Hermes Agent not found. Check your installation."

        except Exception as e:
            logger.error("Unexpected error calling Hermes: %s", e, exc_info=True)
            self._kill_process()
            return f"❌ Unexpected error: {str(e)[:200]}"

        finally:
            self._process = None
            done.set()

    def _kill_process(self) -> None:
        """Terminate the running hermes subprocess (if any)."""
        proc = self._process
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    def stop(self) -> bool:
        """
        Manually stop a running hermes subprocess (the `/stop` command).

        Returns True if a process was killed, False if nothing was running.
        """
        proc = self._process
        if proc is None or proc.poll() is not None:
            return False
        self._stop_requested = True
        logger.info("Stop requested — killing hermes subprocess")
        self._kill_process()
        return True

    def set_model(self, model: str | None) -> str:
        """
        Change the pinned model at runtime (used by the `/model` command).

        Returns the new active model string for confirmation.
        """
        with self._model_lock:
            self.model = (model or "").strip() or None
            active = self.model
        logger.info("Hermes model set to: %s", active)
        return active or "(hermes default)"

    def get_model(self) -> str:
        with self._model_lock:
            return self.model or "(hermes default)"

    def reset_session(self) -> None:
        """
        End the current conversation thread so the next message starts fresh.

        We can't delete the named thread in-place, so we bump the session name
        and clear the pinned ID — the next `chat()` call opens a brand-new
        thread with no prior context. This is what the `/new` command triggers.
        """
        import time

        self.session_name = f"mesh-{self.source_tag}-{int(time.time())}"
        self._resume_id = None
        logger.info("Session reset — new thread: %s", self.session_name)

    def _error_reply(self, returncode: int, stderr: str) -> str:
        """Build a user-friendly error message from a failed Hermes call."""
        if returncode == 1:
            return "❌ Error processing message. Check the logs."
        elif returncode == 130:
            return "⏱️ Processing canceled due to timeout."
        else:
            brief = (stderr or "").split("\n")[-1][:200]
            if brief:
                return f"❌ Error (code {returncode}): {brief}"
            return f"❌ Error (code {returncode})"
