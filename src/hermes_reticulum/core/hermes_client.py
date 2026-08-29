"""
Hermes Integration — calls Hermes Agent to process messages and return replies.

Supports two modes:
  1. CLI mode (default): calls `hermes chat -q` as a subprocess
  2. Future: direct Python import when Hermes is importable
"""

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time

logger = logging.getLogger("hermes_reticulum.hermes")

# Where the CLI-side tool watcher writes its diagnostic log. The hook's
# ~/.hermes/logs/mesh-tool-events.log is the gateway's; this one proves the
# bridge's own watcher is firing for the mesh child (which the hook never
# sees, because agent:step only fires in the gateway).
_STEP_DIAG_LOG = os.path.expanduser(
    os.environ.get("HERMES_STEP_DIAG_LOG", "~/.hermes/logs/mesh-bridge-step.log")
)


def _diag(msg: str) -> None:
    """Append a one-line diagnostic to the step log (best-effort, never raises)."""
    try:
        Path = __import__("pathlib").Path
        p = Path(_STEP_DIAG_LOG)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001 — diagnostics must never break a turn
        pass

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
        # Liveness-heartbeat marker file (spec Part 1, Option A): the
        # ``agent:step`` hook and the bridge itself write
        # ``{"session": <thread title>, "ts": <time.time()>, "phase": ...}``
        # here, and the per-child watcher in _run_with_liveness_guard reads
        # it every second. A fresh marker (same session, ts within the
        # window) touches the liveness clock, turning the guard from a
        # max-turn wall clock into a *stall* detector: a slow-but-working
        # model keeps the marker warm and runs to completion; a wedged one
        # stops touching it and is killed.
        #
        # File mtime is authoritative (survives a write being swapped out
        # of the page cache between reads); the JSON "ts" is informational.
        self.turn_alive_file = os.path.expanduser(
            os.getenv(
                "HERMES_TURN_ALIVE_FILE",
                "~/.hermes/.reticulum-turn-alive",
            )
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
        # Set True by stop() when the kill came from a mesh operator veto
        # (on_deny), as opposed to a liveness-guard kill or /stop. A veto
        # must never be auto-retried — a denied tool re-denies forever.
        self._deny_veto = False
        # Serialize turns per model: two bridge turns must not race the same
        # local model (they would queue on the GPU and look dead to the
        # liveness guard). One turn at a time per HermesClient instance.
        self._turn_lock = __import__("threading").Lock()
        # Steering text queued by /steer — injected as a prefix to the
        # *next* prompt (next hermes chat invocation), then cleared.
        self._steer_pending: str | None = None
        # Step-through mode ("print the full tool call and output before the
        # model moves on"). See StepThroughManager in core/bridge.py.
        self._step_mode = False
        # Checkpoint gate: while set, chat() blocks before returning the
        # reply until /go (or the hold timeout). Set by /hold.
        self._hold_gate = False
        # Push callback: callable(text: str) → sends a message to the mesh
        # peer. Set by the bridge (cli.py); the hook can't push directly
        # (it runs in the gateway process, not the bridge).
        self._push_callback = None

        logger.info("Hermes binary: %s", self.hermes_bin)
        if self.model:
            logger.info("Hermes model pinned: %s", self.model)
        logger.info("Hermes session thread: %s", self.session_name)
        if self.liveness_timeout:
            logger.info("Liveness guard: %ds (0=off)", self.liveness_timeout)
        logger.debug("Turn-alive heartbeat file: %s", self.turn_alive_file)

    # ── Liveness-heartbeat marker (spec Part 1, Option A) ────────────

    def write_turn_alive_marker(self, phase: str = "model") -> None:
        """Write the "still working" marker for this mesh thread.

        The hook (``agent:step``, in the gateway process) writes this on
        every tool batch; the bridge writes ``phase="model"`` when it
        spawns the child (and on each retry), so a turn that never reaches
        a tool still has a start marker. Best-effort: a failure to write
        degrades to the bytes-only guard (no marker = no touches), never
        raises.
        """
        try:
            tmp = self.turn_alive_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "session": self.session_name,
                        "ts": time.time(),
                        "phase": phase,
                    },
                    f,
                )
            os.replace(tmp, self.turn_alive_file)
        except OSError as exc:
            logger.debug("could not write turn-alive marker: %s", exc)

    def clear_turn_alive_marker(self) -> None:
        """Remove the marker so a dead turn's marker can't leak into the
        next turn (spec item 3)."""
        try:
            os.unlink(self.turn_alive_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("could not clear turn-alive marker: %s", exc)

    def _marker_alive(self) -> bool:
        """True if the marker file is fresh for *this* session.

        Freshness is judged on file **mtime** (not the JSON ``ts``): mtime
        survives a read racing the writer's temp-file swap, and it's what
        the watcher polls every second. Scoped to this child's session —
        a different bridge turn's hook firing must NOT keep this child
        alive.
        """
        if not self.liveness_timeout:
            return False
        try:
            mtime = os.stat(self.turn_alive_file).st_mtime
        except OSError:
            return False
        if time.time() - mtime > self.liveness_timeout:
            return False
        try:
            with open(self.turn_alive_file, encoding="utf-8") as f:
                marker = json.load(f)
        except (OSError, ValueError):
            return False
        return marker.get("session") == self.session_name

    def steer(self, text: str) -> None:
        """Queue steering text to be injected as a prefix to the next prompt."""
        self._steer_pending = (text or "").strip() or None

    def pop_steer(self) -> str | None:
        """Return and clear pending steering text (called before each chat())."""
        text = self._steer_pending
        self._steer_pending = None
        return text or None

    # ── Step-through mode ─────────────────────────────────────────────

    def set_step_mode(self, enabled: bool) -> None:
        """Toggle step-through mode: full tool I/O before the model moves on."""
        self._step_mode = bool(enabled)
        logger.info("Step-through mode: %s", "ON" if enabled else "OFF")

    def _step_mode_file_path(self) -> str:
        """Path of the state file that is the source of truth for step mode.

        The hook (gateway process) and the CLI-side watcher (bridge process)
        both read this; the in-process flag is only a convenience.
        """
        from hermes_reticulum.core.bridge import MODE_STATE_PATH
        return MODE_STATE_PATH

    def is_step_mode(self) -> bool:
        """Step mode: prefer the state file (source of truth) so the hook and
        the CLI-side watcher always agree, then fall back to the flag."""
        try:
            from pathlib import Path
            if Path(self._step_mode_file_path()).read_text().strip() == "1":
                self._step_mode = True
        except (OSError, Exception):
            pass
        return self._step_mode

    def set_hold_gate(self, enabled: bool) -> None:
        """Enable/disable the checkpoint gate (set by /hold; /go releases)."""
        self._hold_gate = bool(enabled)

    def set_push_callback(self, callback) -> None:
        """Register a push callback: callable(text: str) → send to mesh peer."""
        self._push_callback = callback

    # ── CLI-side step-through watcher ────────────────────────────────
    #
    # WHY THIS EXISTS: the ``agent:step`` hook only fires in the *gateway*
    # (gateway/run.py wires agent.step_callback). The bridge's child is a
    # ``hermes chat -q`` CLI process, so that hook NEVER runs for a mesh
    # turn — which is why reformatting the hook's payload changed nothing
    # on the mesh. Instead, the bridge (this process, which owns the live
    # push path) polls state.db for new tool rows while the child runs and
    # pushes each tool call + its output as its own 💻 message, exactly
    # like the gateway's progress bubble. No gateway, no hook needed.

    def _push_step(self, name: str, args_raw, result: str, is_error: bool) -> None:
        """Push one tool call (💻 + command) and its output to the mesh peer."""
        if not self._push_callback:
            _diag(f"  push_step({name}) skipped: no push callback")
            return
        # Primary argument, formatted for the mesh (gateway 💻 style).
        args_text = args_raw or ""
        if isinstance(args_raw, (dict, list)):
            try:
                args_text = json.dumps(args_raw, ensure_ascii=False)
            except (TypeError, ValueError):
                args_text = str(args_raw)
        if name == "terminal" and isinstance(args_raw, dict):
            primary = args_raw.get("command") or args_raw.get("cmd")
            if primary:
                args_text = str(primary)
        if not args_text:
            args_text = "(no arguments)"
        # Error tools get a ❌ prefix; keep the command visible either way.
        head = f"❌ {name}" if is_error else f"💻 {name}"
        body = f"{head}\n{args_text}"
        if result:
            body += f"\n{result}"
        _diag(f"  push_step({name}) → {len(body)} chars")
        try:
            self._push_callback(body)
        except Exception as e:  # noqa: BLE001 — a push failure must not kill the turn
            _diag(f"  push_step({name}) callback raised: {e}")
            logger.warning("Step push failed for %s: %s", name, e)

    def _run_step_watcher(self, sid: str, stop_evt: threading.Event) -> None:
        """Poll state.db for new tool rows in this session and push each.

        Runs in a daemon thread for the lifetime of one chat() turn. Tracks
        the last pushed row id so a tool batch is pushed exactly once.
        Best-effort: any error is logged and the loop continues.
        """
        db_path = os.path.expanduser(
            os.environ.get("HERMES_STATE_DB", "~/.hermes/state.db")
        )
        _diag(f"step-watcher start sid={sid}")
        # Seed at the session's existing MAX id so we only push THIS turn's
        # rows. state.db `messages` is session-global, so a fresh watcher on
        # the 2nd+ turn of a session would otherwise replay the whole prior
        # tool history as a one-second burst (recap bug — see
        # docs/mesh-bridge-findings-2026-08-29-step-watcher-recap-bug.md).
        # Safe to seed at start: the user message is persisted before the
        # watcher runs, so rows with id <= seed belong to earlier turns.
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT MAX(id) FROM messages WHERE session_id = ?", (sid,)
            ).fetchone()
            last_pushed = row[0] if row and row[0] else 0
            conn.close()
        except (sqlite3.Error, OSError):
            last_pushed = 0
        pushed = 0
        while not stop_evt.is_set():
            if stop_evt.wait(1.0):
                break
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    row = conn.execute(
                        "SELECT MAX(id) FROM messages WHERE session_id = ?",
                        (sid,),
                    ).fetchone()
                    last_id = row[0] if row and row[0] else 0
                finally:
                    conn.close()
            except (sqlite3.Error, OSError) as e:
                _diag(f"step-watcher query error: {e}")
                continue
            if last_id is None or last_id <= last_pushed:
                continue
            # New rows since the last push — fetch the tail (all roles) and
            # extract tool calls (assistant rows) + their results
            # (role='tool' rows, linked by tool_call_id).
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    rows = conn.execute(
                        "SELECT id, role, tool_calls, tool_name, content, "
                        "tool_call_id "
                        "FROM messages WHERE session_id = ? AND id > ? "
                        "ORDER BY id ASC",
                        (sid, last_pushed),
                    ).fetchall()
                finally:
                    conn.close()
            except (sqlite3.Error, OSError) as e:
                _diag(f"step-watcher fetch error: {e}")
                continue
            results_by_cid: dict = {}
            for rid, role, tool_calls, tool_name, content, tool_call_id in rows:
                if role == "tool" and tool_call_id:
                    results_by_cid[tool_call_id] = content or ""
                elif role == "assistant" and tool_calls:
                    try:
                        calls = json.loads(tool_calls)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(calls, list):
                        continue
                    for call in calls:
                        if not isinstance(call, dict) or "function" not in call:
                            continue
                        fn = call.get("function") or {}
                        cname = fn.get("name", "?")
                        cargs = fn.get("arguments")
                        cid = call.get("id") or call.get("call_id")
                        result_text = results_by_cid.get(cid, "")
                        is_error = bool(result_text) and result_text.strip().startswith(
                            ("❌", "Error", "error:")
                        )
                        self._push_step(cname, cargs, result_text, is_error)
                        pushed += 1
            last_pushed = last_id
        _diag(f"step-watcher stop sid={sid} pushed={pushed}")

    def _step_prompt_prefix(self) -> str | None:
        """Prefix injected when step-through mode is ON.

        The hook (agent:step) can only fire AFTER a tool batch runs — it
        can't veto.  So the "show before next action" part is handled by
        instructing the model to announce each tool it's about to run,
        and the hook delivers the FULL tool call + full output chunked
        across multiple LXMF posts as the turn progresses.
        """
        if not self._step_mode:
            return None
        return (
            "[Step-through mode is ACTIVE.]\n"
            "You are being watched over the mesh, one action at a time.\n"
            "Tool calls and their full output are delivered to the user as\n"
            "their own separate messages automatically — do NOT restate,\n"
            "summarize, or narrate tool calls, tool arguments, or tool\n"
            "output in your reply (no 'Step 1 result...', no 'the command\n"
            "returned...'). Just run the tools and use the results.\n"
            "Your final message must be the answer only, in a few short\n"
            "lines, with no recap of the tool activity.\n\n"
        )

    def _apply_hold_gate(self, reply: str | None) -> str | None:
        """If /hold was requested, gate the reply until /go or timeout."""
        if not self._hold_gate:
            return reply
        if self._push_callback:
            self._push_callback(
                "⏸ Held — send /go to release (auto-releases in ~30 min)"
            )
        # Block until released or timeout. The /go handler (running in a
        # different thread) sets _hold_gate=False.
        deadline = time.time() + 1800
        while self._hold_gate and time.time() < deadline:
            time.sleep(1)
        self._hold_gate = False
        return reply

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

    def _guard_kill_worth_retrying(self, run_ms: float) -> bool:
        """A guard-kill is worth a single resume-retry only if the first run
        died *early* — i.e. it actually consumed less than a full liveness
        window. A child that already ran for a full window is a *slow* model,
        not a wedged one: the turn did real (expensive, often irreversible)
        work, and a resume re-runs that same work, re-burning a full window
        and failing identically (waste, and the user waits 2× the window).

        ``run_ms`` is how long the first subprocess lived. With
        ``liveness_timeout = N`` seconds, the guard can only fire at
        ``≥ N`` seconds of silence, so ``run_ms < N*1000`` is the
        "died before consuming a full window" test. A fresh/wedged model
        dies right at ~N; a slow-but-working one dies *after* N+ε — both
        are worth the retry. A model that ran *past* N (e.g. a 900s turn on
        a 600s window) is NOT.
        """
        return run_ms < self.liveness_timeout * 1000

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

        # Step-through mode: instruct the model to NOT narrate step-by-step —
        # the CLI-side watcher delivers each tool call + output as its own
        # mesh message (the equivalent of the gateway's 💻 progress bubble).
        # is_step_mode() reads the state file (source of truth), so this works
        # even if the in-process flag was never set on this process.
        if self.is_step_mode():
            step_prefix = self._step_prompt_prefix()
            if step_prefix:
                message = f"{step_prefix}{message}"

        cmd = [
            self.hermes_bin,
            "chat",
            "-q",
            message,
            "--source",
            self.source_tag,
            "-Q",  # quiet — suppress banner/spinner
        ]
        if self._resume_id:
            cmd += ["--resume", self._resume_id]
        else:
            cmd += ["-c", self.session_name, "--create-if-missing"]
        with self._model_lock:
            model = self.model
        if model:
            cmd += ["-m", model]
        cmd.extend(self.extra_args)

        # CLI-side step-through watcher: push each tool call + output as its
        # own mesh message while the child runs. The gateway hook (agent:step)
        # can't fire for a CLI child, so this is what actually delivers the
        # per-tool 💻 messages the user sees. Gated on step-mode ON + a push
        # callback (only the bridge sets one).
        watcher_stop = threading.Event()
        watcher_sid = None
        if self.is_step_mode() and self._push_callback:
            watcher_sid = self._resume_id or self._resolve_session_id()
            _diag(
                f"step-mode ON, push set, sid={watcher_sid} "
                f"resume_id={self._resume_id}"
            )
            threading.Thread(
                target=self._run_step_watcher,
                args=(watcher_sid, watcher_stop),
                daemon=True,
                name="hermes-step-watcher",
            ).start()
        else:
            _diag(
                f"step watcher NOT started (step_mode={self.is_step_mode()}, "
                f"push={self._push_callback is not None})"
            )

        try:
            logger.debug("Calling: %s", " ".join(cmd))
            self.set_last_prompt(message)
            # Serialize turns per model: wait for an in-flight turn (on this
            # HermesClient) instead of racing the same local model. The
            # liveness clock inside _run_with_liveness_guard is per-subprocess,
            # so waiting for the lock here does not trip the guard — the child
            # only starts (and the clock only starts) once we hold the lock.
            self._deny_veto = False
            with self._turn_lock:
                result = self._run_with_liveness_guard(cmd)
            if result is not None and self._guard_killed and not self._deny_veto:
                if self._guard_kill_worth_retrying(
                    getattr(self, "_last_run_ms", 0.0)
                ):
                    # The liveness guard SIGKILL'd the child *early* (it never
                    # consumed a full window) — that's a wedged/stalled model,
                    # and the prompt is already persisted in the session, so
                    # resume and retry exactly once. A wedged model often
                    # recovers.
                    logger.warning(
                        "Liveness guard killed the child early "
                        "(%.0fms < %ds window); retrying once",
                        getattr(self, "_last_run_ms", 0.0),
                        self.liveness_timeout,
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
                else:
                    # The child ran a *full* window before the guard fired:
                    # the model was slow but working, not wedged. The prompt
                    # is persisted, but a resume would re-run the same (real,
                    # often irreversible) work and re-burn a full window —
                    # wasting 2× the wait and failing identically. Report
                    # honestly and let the user retry / re-send.
                    logger.warning(
                        "Liveness guard killed the child after a full "
                        "%.0fms window — model was slow, not wedged; "
                        "skipping retry (would re-burn the window)"
                    )
                    result = (
                        f"⏱️ Turn ran past the liveness window "
                        f"({self.liveness_timeout}s) — the model was still "
                        f"working but the turn took too long. Re-send to "
                        f"continue; or lower HERMES_LIVENESS_TIMEOUT."
                    )
            # Stop the step watcher now the child has finished (it only ever
            # pushes rows created during the turn).
            watcher_stop.set()
            # Checkpoint gate: if the operator pressed /hold, block here
            # until /go (or timeout) before the reply leaves the bridge.
            result = self._apply_hold_gate(result)
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
        started = time.monotonic()
        activity_lock = threading.Lock()
        done = threading.Event()
        self._guard_killed = False
        marker_touched = False  # debug: marker-driven touches are logged once

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
                        "Liveness guard: no output or heartbeat for %ds, "
                        "killing hermes",
                        self.liveness_timeout,
                    )
                    self._stop_requested = True
                    self._guard_killed = True
                    # A liveness-guard kill is NOT a mesh veto — clear it so
                    # chat()'s early-death retry still works for a genuinely
                    # wedged model. A mesh operator veto (stop() →
                    # _deny_veto=True) must survive and suppress the retry,
                    # or a denied tool re-denies in a loop.
                    self._deny_veto = False
                    self._kill_process()
                    self.clear_turn_alive_marker()
                    return
                # Liveness-heartbeat marker (spec Part 1, Option A): a fresh
                # marker for THIS session means the model made tool progress
                # (or at least the turn started) within the window — touch the
                # liveness clock, turning the guard into a stall detector
                # instead of a wall clock. The session match is what scopes
                # this to our child: another bridge turn's hook firing can't
                # keep ours alive.
                if self._marker_alive():
                    nonlocal marker_touched
                    if not marker_touched:
                        marker_touched = True
                        logger.debug(
                            "Liveness heartbeat: turn-alive marker fresh "
                            "(session=%r, file=%s) — touching clock",
                            self.session_name, self.turn_alive_file,
                        )
                    _touch()
                done.wait(min(remaining, 1.0))

        proc: subprocess.Popen | None = None
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        try:
            # Spec Part 1: reset the marker at spawn so a dead turn's stale
            # marker can't leak into this one, then write a fresh
            # phase="model" marker so even a turn that never reaches a tool
            # has a start marker (and the guard has something to fall back
            # to — a fresh start marker covers at most one full window of
            # pure model thinking before the hook must take over).
            self.clear_turn_alive_marker()
            self.write_turn_alive_marker(phase="model")
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
            # How long the child lived — chat() uses this to decide whether a
            # guard-kill is worth a resume-retry (early death = wedged model;
            # ran a full window = slow model, a retry would re-burn it).
            self._last_run_ms = (time.monotonic() - started) * 1000.0

            if proc.returncode != 0:
                logger.error(
                    "Hermes exited with code %d: %s",
                    proc.returncode,
                    stderr[:500] if stderr else "(no stderr)",
                )
                if self._stop_requested and not self._guard_killed:
                    # Deliberate /stop or an on_deny veto: report honestly —
                    # the model may still have been working fine.
                    return "⏹️ Stopped before the turn finished."
                if self._guard_killed:
                    # "We killed it" (liveness guard or /stop), not a hermes
                    # crash: give the user an honest message, and the caller
                    # will auto-retry once (see chat()). With the heartbeat
                    # marker in place, "no output" also means "no tool
                    # progress" — a slow-but-working model is kept alive by
                    # the marker and only dies here when it truly stalled.
                    return (
                        f"⏱️ Turn exceeded the liveness window "
                        f"({self.liveness_timeout}s) with no output and no "
                        f"tool progress — the model may be busy or stalled. "
                        f"Please retry."
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

        Live tool steps are the primary channel; this is the safety net —
        if the live path is disabled or a hermes update changes the event
        surface, the mesh still sees which tools ran.

        Suppressed entirely in step mode: the CLI-side watcher already
        delivered each tool call + output as its own 💻 message, so a recap
        footer would be a redundant recap the user explicitly rejected.
        """
        if self._step_mode:
            _diag(f"  recap suppressed (step mode) reply={len(reply)} chars")
            return reply
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
        """Terminate the running hermes subprocess (if any).

        Also clears the liveness marker (spec item 3): a killed turn's
        marker must not leak into the next turn. The watcher's kill path
        clears it too (belt-and-suspenders); this covers /stop and
        external kills.
        """
        proc = self._process
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        self.clear_turn_alive_marker()

    def stop(self) -> bool:
        """
        Manually stop a running hermes subprocess (the `/stop` command).

        Returns True if a process was killed, False if nothing was running.
        """
        proc = self._process
        if proc is None or proc.poll() is not None:
            return False
        self._stop_requested = True
        # _guard_killed stays False: a deliberate /stop (or an on_deny
        # veto) is "stopped", not a liveness failure. The exit path uses
        # this to report ⏹️ instead of the liveness message or code -9.
        # _deny_veto marks this specifically as a mesh operator veto so
        # chat() never auto-retries it (a denied tool re-denies forever).
        self._guard_killed = False
        self._deny_veto = True
        logger.info("Stop requested — killing hermes subprocess")
        self._kill_process()
        # Spec Part 1: a dead turn's stale marker must not leak into the next
        # turn (a wedged marker would otherwise keep the next child warm).
        self.clear_turn_alive_marker()
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
