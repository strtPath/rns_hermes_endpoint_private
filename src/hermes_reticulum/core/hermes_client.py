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
        # NOTE: in `-q` mode the child is silent for the entire model turn, so
        # this is effectively a max-turn-latency wall clock. 600s is the floor
        # for realistic single-turn latency on a local 27B-class model (observed
        # 100-950s turns).
        self.liveness_timeout = int(
            os.getenv("HERMES_LIVENESS_TIMEOUT", "600")
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
        self._guard_killed = False
        # Serialize turns per model: two bridge turns must not race the same
        # local model (they would queue on the GPU and look dead to the
        # liveness guard). One turn at a time per HermesClient instance.
        self._turn_lock = __import__("threading").Lock()
        # Steering text queued by /steer — injected as a prefix to the
        # *next* prompt (next hermes chat invocation), then cleared.
        self._steer_pending: str | None = None

        logger.info("Hermes binary: %s", self.hermes_bin)
        if self.model:
            logger.info("Hermes model pinned: %s", self.model)
        logger.info("Hermes session thread: %s", self.session_name)
        if self.liveness_timeout:
            logger.info("Liveness guard: %ds (0=off)", self.liveness_timeout)

    def steer(self, text: str) -> None:
        """Queue steering text to be injected as a prefix to the next prompt."""
        self._steer_pending = (text or "").strip() or None

    def pop_steer(self) -> str | None:
        """Return and clear pending steering text (called before each chat())."""
        text = self._steer_pending
        self._steer_pending = None
        return text or None

    def tool_recap(self, limit: int = 10) -> list[dict]:
        """Recap of the last tool calls in the current mesh session.

        Read from state.db (the same store hermes persists to), so this
        works even when the live ``agent:step`` hook is unavailable.
        Returns a list of ``{"name": str, "is_error": bool, "preview": str}``
        for the most recent ``limit`` tool messages (oldest first).
        """
        import json
        import sqlite3

        sid = self._resume_id or self._resolve_session_id()
        if not sid:
            return []
        db_path = os.path.expanduser(
            os.environ.get("HERMES_STATE_DB", "~/.hermes/state.db")
        )
        if not os.path.exists(db_path):
            return []
        out: list[dict] = []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                # Tool-call messages are persisted as assistant rows with a
                # tool_calls JSON payload; tool *results* as role='tool'.
                rows = conn.execute(
                    "SELECT content, tool_calls FROM messages "
                    "WHERE session_id = ? AND role = 'assistant' "
                    "ORDER BY id ASC",
                    (sid,),
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("tool_recap: state.db query failed: %s", exc)
            return []
        for content, tool_calls_json in rows:
            if not tool_calls_json:
                continue
            try:
                calls = json.loads(tool_calls_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(calls, list):
                continue
            for call in calls:
                if isinstance(call, dict) and call.get("function"):
                    name = call["function"].get("name", "?")
                    out.append({
                        "name": name,
                        "is_error": False,
                        "preview": str(call["function"].get("arguments", ""))[:120],
                    })
        return out[-limit:]

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

        # Steering from /steer: prefix the next prompt with the operator's
        # queued instruction (consumed once).
        steer = self.pop_steer()
        if steer:
            message = f"[Operator steering] {steer}\n\n{message}"
            logger.info("Injected steering: %s", steer[:80])

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
            self.set_last_prompt(message)
            # Serialize turns per model: wait for an in-flight turn (on this
            # HermesClient) instead of racing the same local model. The
            # liveness clock inside _run_with_liveness_guard is per-subprocess,
            # so waiting for the lock here does not trip the guard — the child
            # only starts (and the clock only starts) once we hold the lock.
            with self._turn_lock:
                result = self._run_with_liveness_guard(cmd)
            if result is not None and self._guard_killed:
                # The liveness guard SIGKILL'd the child mid-turn. The prompt
                # is already persisted in the session, so resume and retry
                # exactly once — a wedged/stalled model often recovers.
                logger.warning(
                    "Liveness guard killed the child; retrying once"
                )
                time.sleep(5)
                self._ensure_session(False)
                with self._model_lock:
                    model = self.model
                cmd = [
                    self.hermes_bin, "chat", "-q", message,
                    "--source", self.source_tag, "-Q",
                ]
                if self._resume_id:
                    cmd += ["--resume", self._resume_id]
                elif not self._resolve_session_id():
                    cmd += ["-c", self.session_name, "--create-if-missing"]
                if model:
                    cmd += ["-m", model]
                cmd.extend(self.extra_args)
                with self._turn_lock:
                    result = self._run_with_liveness_guard(cmd)
            return result

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
        self._guard_killed = False

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
                    self._guard_killed = True
                    self._stop_requested = True
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
                if self._guard_killed:
                    # "We killed it" (liveness guard or /stop), not a hermes
                    # crash: give the user an honest message, and the caller
                    # will auto-retry once (see chat()).
                    return (
                        f"⏱️ Turn exceeded the liveness window "
                        f"({self.liveness_timeout}s) — the model may be "
                        f"busy or stalled. Please retry."
                    )
                return self._error_reply(proc.returncode, stderr)

            reply = stdout.strip()
            if not reply:
                logger.warning("Hermes returned empty output")
                return "_(no response)_"
            return self._with_tool_recap(reply)

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

    def _with_tool_recap(self, reply: str) -> str:
        """Append a compact tool recap to the reply (recap fallback).

        Live ``agent:step`` hook events are the primary channel; this is the
        safety net — if the hook is disabled or a hermes update changes the
        event surface, the mesh still sees which tools ran.
        """
        try:
            recap = self.tool_recap(limit=8)
        except Exception as exc:
            logger.debug("tool_recap failed: %s", exc)
            return reply
        if not recap:
            return reply
        names = [r["name"] for r in recap]
        line = "🔧 " + ", ".join(names)
        if len(names) > 8:
            line += f" (+{len(names) - 8} more)"
        return f"{reply}\n\n_{line}_"

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

    def is_running(self) -> bool:
        """True while a hermes subprocess is actively running."""
        proc = self._process
        return proc is not None and proc.poll() is None

    def set_last_prompt(self, prompt: str) -> None:
        """Remember the last user prompt sent to hermes (for /retry)."""
        self._last_prompt = (prompt or "").strip() or None

    def get_last_prompt(self) -> str | None:
        """The last user prompt sent to hermes, or None."""
        return getattr(self, "_last_prompt", None)

    def pause(self, reason: str = "") -> str:
        """
        Engage Hermes' global emergency stop (``hermes pause``).

        Halts new work (cron/kanban dispatch, new gateway turns) until
        ``resume()`` is called. In-flight subprocesses are NOT killed —
        use ``stop()`` for that.
        """
        cmd = [self.hermes_bin, "pause"]
        if reason:
            cmd += ["--reason", reason]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return f"⚠️ pause failed: {result.stderr.strip()[:200]}"
            return f"⏸️ Hermes paused. New work halted. Use /resume to lift."
        except Exception as e:
            return f"⚠️ pause error: {e}"

    def resume(self) -> str:
        """Lift the global emergency stop set by ``pause()``."""
        try:
            result = subprocess.run(
                [self.hermes_bin, "resume"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return f"⚠️ resume failed: {result.stderr.strip()[:200]}"
            return "▶️ Hermes resumed. New work allowed again."
        except Exception as e:
            return f"⚠️ resume error: {e}"

    def version(self) -> str:
        """
        Return the running Hermes Agent version string (first line of
        ``hermes --version``), or a fallback on error.
        """
        try:
            result = subprocess.run(
                [self.hermes_bin, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            line = (result.stdout or result.stderr).strip().split("\n")[0]
            return line or "unknown"
        except Exception as e:
            return f"unknown ({e})"

    def session_token_stats(self) -> tuple[int, int, str | None]:
        """
        Read token totals for the current mesh session from state.db.

        Returns (total_tokens, message_count, session_id). All zeros if
        the session has not been persisted yet (e.g. before the first
        turn completes) or the DB is unreadable.
        """
        import sqlite3

        sid = self._resume_id
        if not sid:
            sid = self._resolve_session_id()
        if not sid:
            return 0, 0, None

        db_path = os.path.expanduser(
            os.environ.get("HERMES_STATE_DB", "~/.hermes/state.db")
        )
        if not os.path.exists(db_path):
            return 0, 0, sid

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(token_count), 0), COUNT(*)"
                    " FROM messages"
                    " WHERE session_id = ? AND active = 1 AND compacted = 0",
                    (sid,),
                ).fetchone()
                if row is None:
                    return 0, 0, sid
                return int(row[0]), int(row[1]), sid
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Could not read token stats for %s: %s", sid, exc)
        return 0, 0, sid

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
