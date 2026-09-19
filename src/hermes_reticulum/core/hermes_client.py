"""
Hermes Integration — calls Hermes Agent to process messages and return replies.

Supports two modes:
  1. CLI mode (default): calls `hermes chat -q` as a subprocess
  2. Future: direct Python import when Hermes is importable
"""

import functools
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("hermes_reticulum.hermes")


@functools.lru_cache(maxsize=16)
def hermes_chat_supports_flag(hermes_bin: str, flag: str) -> bool:
    """Whether this ``hermes`` build's ``chat`` subcommand accepts *flag*.

    Flag sets differ across Hermes versions, and an unrecognized flag makes
    ``hermes chat`` exit non-zero with a usage error — which would silently
    break every agent call (the bridge would then return no AI reply at all).
    ``--create-if-missing`` is one such flag: it is NOT present in Hermes
    v0.19.0. Probe once per interpreter and cache the result.

    Fails closed to ``False`` (omit the flag) so an unknown build still runs.
    """
    try:
        proc = subprocess.run(
            [hermes_bin, "chat", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 — never let the probe break a turn
        # Includes OSError/SubprocessError and any monkeypatched-Popen oddity:
        # if we cannot prove the flag exists, omit it (fail closed).
        logger.debug("hermes chat --help probe failed for %s: %s", hermes_bin, exc)
        return False
    output = (proc.stdout or "") + (proc.stderr or "")
    return flag in output

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
        # Hard cap: an absolute wall clock on a single turn, independent of the
        # relative liveness silence window. Even if the marker keeps trickling
        # fresh tool rows forever, a turn that runs past this cap is itself
        # suspect and is killed. Set 0 to disable. Distinct from
        # liveness_timeout (which is "silence with no progress").
        self.turn_hard_cap = int(
            os.getenv("HERMES_TURN_HARD_CAP", "21600")
        )
        # Liveness-heartbeat marker file (spec Part 1, Option A): the
        # ``agent:step`` hook and the bridge itself write
        # ``{"session": <thread title>, "ts": <time.time()>, "phase": ...,
        #    "gen": <int>}`` here, and the per-child watcher in
        # _run_with_liveness_guard reads it every second. A fresh marker
        # (same session, matching generation, ts within the window) touches
        # the liveness clock, turning the guard from a max-turn wall clock
        # into a *stall* detector: a slow-but-working model keeps the marker
        # warm and runs to completion; a wedged one stops touching it and is
        # killed.
        #
        # The per-child generation counter (spec Part 1, Option B, 2026-08-30)
        # scopes the marker to ONE live child. The hook (gateway process)
        # writes the marker for ANY session that resolves to a mesh thread —
        # including the operator's own gateway session, which the mesh hook
        # also streams. Without a generation, the operator's long-running
        # gateway session would keep the marker warm and keep a mesh child
        # alive (or, conversely, the mesh child's marker could be clobbered
        # by the gateway's writes). The bridge bumps a fresh generation on
        # every child spawn (and retry); the hook only refreshes the marker
        # when its gen matches, so a child's heartbeat is scoped to that
        # child's lifetime and a long-running gateway session can't kill a
        # mesh child mid-turn.
        #
        # File mtime is authoritative (survives a write being swapped out
        # of the page cache between reads); the JSON "ts" and "gen" are
        # informational but "gen" is the scope key.
        self.turn_alive_file = os.path.expanduser(
            os.getenv(
                "HERMES_TURN_ALIVE_FILE",
                "~/.hermes/.reticulum-turn-alive",
            )
        )
        self._turn_alive_gen = 0
        self._turn_alive_lock = threading.Lock()
        self.source_tag = source_tag
        self.extra_args = extra_args or []
        self.model = model or os.getenv("HERMES_MODEL", "").strip() or None
        self.session_name = os.getenv(
            "HERMES_SESSION_NAME", f"mesh-{source_tag}"
        )
        self._model_lock = threading.Lock()
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
        self._turn_lock = threading.Lock()
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

    def bump_turn_alive_gen(self) -> int:
        """Advance the per-child generation and return it.

        Call before spawning (or resuming) a child so the child's liveness
        window is scoped to its own lifetime. The hook (gateway process)
        only refreshes the marker when its ``gen`` matches the current
        generation, so a long-running gateway session (the operator's own
        Hermes) can no longer keep a mesh child's liveness clock warm — and
        a mesh child's marker can't be clobbered by the gateway's writes.
        """
        with self._turn_alive_lock:
            self._turn_alive_gen += 1
            return self._turn_alive_gen

    def current_turn_alive_gen(self) -> int:
        with self._turn_alive_lock:
            return self._turn_alive_gen

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
            with self._turn_alive_lock:
                gen = self._turn_alive_gen
            tmp = self.turn_alive_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "session": self.session_name,
                        "ts": time.time(),
                        "phase": phase,
                        "gen": gen,
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
        """True if the marker file is fresh for *this* child.

        Freshness is judged on file **mtime** (not the JSON ``ts``): mtime
        survives a read racing the writer's temp-file swap, and it's what
        the watcher polls every second. Scoped to this child's session AND
        generation — a different bridge turn's hook firing, or the operator's
        own gateway session writing its own marker, must NOT keep this child
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
        if marker.get("session") != self.session_name:
            return False
        # Generation scope: the marker must have been written for the CURRENT
        # child. A stale marker from a previous child (or the gateway's own
        # session) has a different gen and must not count as liveness.
        if marker.get("gen") != self.current_turn_alive_gen():
            return False
        return True

    def _log_marker_scoping_diag(self) -> None:
        """Log the marker's session+gen vs this child's, at a guard kill.

        Distinguishes a scoping mismatch (marker written for a different
        session or generation — e.g. a resumed child whose gen was bumped
        after the hook wrote the marker) from a true stall (no marker at
        all). Called from the watchdog kill path; best-effort, never raises.
        """
        cur_gen = self.current_turn_alive_gen()
        try:
            with open(self.turn_alive_file, encoding="utf-8") as f:
                marker = json.load(f)
        except FileNotFoundError:
            logger.warning(
                "Liveness guard fired: no marker present "
                "(child session=%r gen=%d) — true stall or marker never written",
                self.session_name, cur_gen,
            )
            return
        except (OSError, ValueError) as exc:
            logger.warning(
                "Liveness guard fired: marker unreadable (%s) "
                "(child session=%r gen=%d)",
                exc, self.session_name, cur_gen,
            )
            return
        m_session = marker.get("session")
        m_gen = marker.get("gen")
        if m_session != self.session_name or m_gen != cur_gen:
            logger.warning(
                "Liveness guard fired on SCOPING MISMATCH: marker "
                "(session=%r gen=%s) != child (session=%r gen=%d) — "
                "possibly a stale/resumed marker, not a true stall",
                m_session, m_gen, self.session_name, cur_gen,
            )
        else:
            logger.warning(
                "Liveness guard fired on TRUE STALL: marker matched "
                "(session=%r gen=%d) but went stale within %ds",
                self.session_name, cur_gen, self.liveness_timeout,
            )

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
            if Path(self._step_mode_file_path()).read_text().strip() == "1":
                self._step_mode = True
        except OSError:
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
        """Push one tool call (emoji + name) and its output to the mesh peer."""
        if not self._push_callback:
            _diag(f"  push_step({name}) skipped: no push callback")
            return
        # Primary argument, formatted for the mesh (gateway progress-bubble style).
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
        # Same per-tool emoji the gateway shows on Telegram; ❌ for a failed call.
        from hermes_reticulum.core.tool_emoji import tool_label
        head = tool_label(name, is_error)
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
            saw_tool_activity = False
            for rid, role, tool_calls, tool_name, content, tool_call_id in rows:
                if role == "tool" and tool_call_id:
                    # A tool RESULT row: the model's last call ran. This is
                    # real tool progress — mark it (heartbeat below) and keep
                    # the result for the assistant row that issued it.
                    saw_tool_activity = True
                    results_by_cid[tool_call_id] = content or ""
                elif role == "assistant" and tool_calls:
                    try:
                        calls = json.loads(tool_calls)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(calls, list):
                        continue
                    if calls:
                        saw_tool_activity = True
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
            # Only refresh the liveness marker if this batch actually contained
            # tool activity. MAX(id) advances for ANY new row in the session
            # (user, system, text-only assistant, tool), so a blind refresh on
            # last_id > last_pushed would let unrelated rows keep a stalled
            # child alive until the hard cap. A working turn emits tool rows;
            # only those should warm the heartbeat. The gateway agent:step hook
            # never fires for a CLI child, so this in-process refresh is what
            # keeps a working -q turn's marker fresh (scoped by session + gen).
            # Best-effort: a write failure degrades to the bytes-only guard.
            if saw_tool_activity:
                self.write_turn_alive_marker(phase="tool")
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

    def tool_recap(
        self,
        limit: int = 10,
        anchor: tuple[str, int] | None = None,
    ) -> list[dict]:
        """Recap of the tool calls made during the CURRENT mesh turn.

        Read from state.db (the same store hermes persists to), so this
        works even when the live ``agent:step`` hook is unavailable.
        Scoped to rows with ``id > anchor_id`` (where ``anchor`` is the
        per-turn ``(sid, row_id)`` boundary captured just before this turn's
        child spawned), so a tool-free turn returns [] (no footer) and prior
        turns' tools never bleed in. ``anchor`` is per-call — it is captured
        inside the turn lock and threaded to the recap, NOT stored as shared
        mutable client state, so concurrent bridge turns can never read
        each other's boundary (see
        docs/mesh-bridge-findings-2026-09-15-tool-recap-prior-turns.md).
        If ``anchor`` is None (session not resolvable, or a DB error at
        capture time) or its sid doesn't match the session being recap'd,
        fall back to no filter rather than guessing.

        Returns a list of ``{"name": str, "is_error": bool, "preview": str}``
        for the most recent ``limit`` tool messages of this turn (oldest
        first).
        """
        sid = self._resume_id or self._resolve_session_id()
        if not sid:
            return []
        anchor_sid, anchor_id = (
            (anchor[0], anchor[1]) if anchor else (None, None)
        )
        if anchor_sid == sid and anchor_id is not None:
            filter_anchor = anchor_id
        else:
            filter_anchor = None
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
                if filter_anchor is not None:
                    rows = conn.execute(
                        "SELECT content, tool_calls FROM messages "
                        "WHERE session_id = ? AND role = 'assistant' "
                        "AND id > ? ORDER BY id ASC",
                        (sid, filter_anchor),
                    ).fetchall()
                else:
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
                        "preview": str(
                            call["function"].get("arguments", "")
                        )[:120],
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
                    # NOTE: 'last_activity_at' does not exist in every Hermes
                    # schema (v0.19.0 has 'started_at'); ordering by a missing
                    # column raises and silently breaks thread resumption.
                    "ORDER BY started_at DESC LIMIT 1",
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

    # ── Session creation compatibility (Hermes builds without
    #    --create-if-missing, e.g. v0.19.0) ────────────────────────────────

    def _state_db_path(self) -> str:
        return os.path.expanduser(
            os.environ.get("HERMES_STATE_DB", "~/.hermes/state.db")
        )

    def _session_ids(self) -> set[str]:
        """Snapshot of all session ids, used to detect the session a fresh
        (no ``-c``) run creates."""
        db_path = self._state_db_path()
        if not os.path.exists(db_path):
            return set()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                return {row[0] for row in conn.execute("SELECT id FROM sessions")}
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Could not snapshot sessions from state.db: %s", exc)
            return set()

    def _capture_turn_anchor(
        self, sid: str | None, boundary: str = "max"
    ) -> tuple[str, int] | None:
        """Compute this turn's recap anchor — the ``(sid, row_id)`` boundary
        below which all rows belong to PRIOR turns.

        ``messages`` is session-global (the store is shared across turns), so
        the boundary between "prior turns" and "this turn" must be computed
        at the right moment:

        - boundary="max" (default): the session's MAX(messages.id). Only
          valid BEFORE the child spawns (chat() computes this): at that point
          the max id is the last row of prior turns, and every row the
          child writes has an id above it. A tool-free turn then yields an
          empty recap (no footer) and prior turns' tools never bleed in.
        - boundary="first_user": the session's first role='user' row id.
          Used by _adopt_new_session, where the sid only became known AFTER
          the child ran. The session is brand-new (created by this turn), so
          its first user row is the pre-turn boundary; every tool row of
          this turn sits above it.

        ``tool_recap(anchor=...)`` counts only rows with ``id > row_id``.

        Returns the ``tuple`` so the caller can hold it as a LOCAL variable
        for this turn and thread it into the recap. It is deliberately NOT
        stored as shared client state: chat() may run concurrently from the
        bridge's thread pool, and a shared mutable anchor would let one turn
        overwrite another's boundary (see
        docs/mesh-bridge-findings-2026-09-15-tool-recap-prior-turns.md).

        ``sid`` may be None (session not yet resolvable) → returns None, and
        tool_recap() falls back to no filter (the old behavior) rather than
        guessing. A DB error likewise returns None.
        """
        if not sid:
            return None
        db_path = self._state_db_path()
        if not os.path.exists(db_path):
            return None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                if boundary == "first_user":
                    row = conn.execute(
                        "SELECT MIN(id) FROM messages "
                        "WHERE session_id = ? AND role = 'user'",
                        (sid,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT MAX(id) FROM messages WHERE session_id = ?",
                        (sid,),
                    ).fetchone()
            finally:
                conn.close()
            row_id = row[0] if row and row[0] is not None else 0
            return (sid, row_id)
        except (sqlite3.Error, OSError) as exc:
            logger.debug("Could not capture turn anchor: %s", exc)
            return None

    def _adopt_new_session(
        self, turn_start: float, before: set[str]
    ) -> tuple[str, int] | None:
        """Title the session created by *this* turn with the mesh thread name.

        Compatibility shim for Hermes builds that lack ``--create-if-missing``:
        the first turn runs with no ``-c`` (creating an untitled session), then
        we find that session and rename it via
        ``hermes sessions rename <id> <name>``. Later turns resume it by name.

        Correlation — the session MUST be attributable to this invocation.
        ``state.db`` is shared with the gateway and with any local
        ``hermes chat``, so "the newest session that wasn't here before" is NOT
        safe: a session created elsewhere during our run would be renamed and
        pinned, and later mesh messages would resume an unrelated conversation,
        mixing context between senders. So a candidate must satisfy ALL of:

          * ``started_at >= turn_start``    — created during this turn
          * ``source == self.source_tag``   — matches our ``--source`` flag
          * absent from ``before``          — extra guard against clock skew

        ``cwd`` is deliberately NOT used: Hermes leaves it ``NULL`` for
        sessions created by ``chat -q`` (verified against v0.19.0), so
        filtering on it matches nothing.

        If that does not identify exactly one session, we adopt nothing. An
        unpinned thread costs only continuity; pinning the wrong session
        corrupts the conversation. Callers hold the turn lock across
        spawn+adopt, so concurrent turns on this client cannot interleave.

        Returns the first-turn recap anchor (``boundary="first_user"``) so the
        caller can thread it into the recap; None when nothing was adopted or
        the boundary could not be computed.
        """
        db_path = self._state_db_path()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT id FROM sessions "
                    "WHERE started_at >= ? AND source = ? "
                    "ORDER BY started_at ASC",
                    (turn_start, self.source_tag),
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Could not query state.db to adopt a session: %s", exc)
            return None

        candidates = [row[0] for row in rows if row[0] not in before]
        if len(candidates) != 1:
            logger.warning(
                "Refusing to adopt a session for %r: expected exactly 1 session "
                "created by this turn (source=%r), found %d — leaving the "
                "thread unpinned rather than risk resuming an unrelated "
                "conversation.",
                self.session_name, self.source_tag, len(candidates),
            )
            return None

        session_id = candidates[0]
        try:
            proc = subprocess.run(
                [self.hermes_bin, "sessions", "rename", session_id, self.session_name],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Could not rename session %s: %s", session_id, exc)
            return None
        if proc.returncode != 0:
            logger.warning(
                "hermes sessions rename failed (rc=%s): %s",
                proc.returncode,
                (proc.stderr or proc.stdout or "").strip()[:200],
            )
            return None

        self._resume_id = session_id
        # The session id only became known during this turn (adoption), so
        # the pre-spawn anchor in chat() had no sid. Compute it now with the
        # correct pre-turn boundary: this session is brand-new and was created
        # by THIS turn, so its first user row is the pre-turn boundary — every
        # tool row of this turn has an id above it. (MAX(id) would be wrong:
        # it would exclude this turn's own tools.) Returned as a LOCAL anchor
        # for the caller to thread into the recap (never shared state).
        anchor = self._capture_turn_anchor(session_id, boundary="first_user")
        logger.info(
            "Created and titled mesh thread %r -> session %s",
            self.session_name, session_id[:12],
        )
        return anchor

    def _ensure_session(self, new_session: bool) -> None:
        """
        Maintain the resume target.

        - new_session=True  → start a fresh thread (bump name, clear cache).
        - new_session=False → resolve the named thread to an ID; create the
          titled session once if it doesn't exist yet.

        Sets self._resume_id (the concrete session ID to resume) or None.
        """
        if new_session:
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
        adopt_before: set[str] | None = None
        if self._resume_id:
            cmd += ["--resume", self._resume_id]
        elif hermes_chat_supports_flag(self.hermes_bin, "--create-if-missing"):
            cmd += ["-c", self.session_name, "--create-if-missing"]
        elif self._resolve_session_id():
            cmd += ["-c", self.session_name]
        else:
            # Not pinned, no titled session exists yet, and this Hermes build
            # has no --create-if-missing (v0.19.0). Start a fresh session with
            # no -c, then adopt + title it after the run so later calls can
            # resolve it by name. See _adopt_new_session.
            adopt_before = self._session_ids()
        with self._model_lock:
            model = self.model
        if model:
            cmd += ["-m", model]
        cmd.extend(self.extra_args)

        # Per-turn tool-recap anchor. Computed INSIDE the turn lock (below),
        # held as a LOCAL for this invocation, and threaded into the recap —
        # never shared mutable client state. Two concurrent bridge turns (the
        # bridge fans out on a thread pool) each get their own boundary, so
        # one turn can never read another's. The first turn (sid unknown until
        # adoption) gets None here; _adopt_new_session computes it once the
        # session id is known (boundary="first_user").
        turn_anchor: tuple[str, int] | None = None

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
                # Timestamp the turn BEFORE spawning: it bounds which sessions
                # this invocation can possibly have created (see
                # _adopt_new_session).
                turn_start = time.time()
                # Recap anchor: compute the session's MAX(messages.id) while we
                # hold the turn lock, BEFORE the child spawns, as a LOCAL for
                # this turn. Doing it under the lock (not before) means a
                # concurrent turn can neither overwrite nor read it — the old
                # shared-instance anchor let a second turn clobber the first
                # turn's boundary before either serialized on the lock.
                turn_anchor = self._capture_turn_anchor(self._resume_id)
                # Spec Part 1, Option B: advance the per-child generation
                # before spawning so the hook's refreshes are scoped to THIS
                # child's lifetime, and the initial phase="model" marker
                # (written inside _run_with_liveness_guard) carries the new
                # gen — not a stale one from a previous child.
                self.bump_turn_alive_gen()
                result = self._run_with_liveness_guard(cmd)
                if adopt_before is not None:
                    # The run above started a brand-new untitled session (this
                    # build has no --create-if-missing). Adopt it WHILE STILL
                    # holding the turn lock: state.db is shared, so releasing
                    # the lock first would let a concurrent turn create its own
                    # session inside the spawn→adopt window, and we could pin
                    # the wrong conversation. Adoption also computes the
                    # first-turn anchor (boundary="first_user") now that the
                    # session id exists.
                    adopted_anchor = self._adopt_new_session(
                        turn_start, adopt_before
                    )
                    if adopted_anchor is not None:
                        turn_anchor = adopted_anchor
                # Recap AFTER adoption, so the first-turn anchor (computed
                # during adoption) is already in place when the footer is
                # built. On a resumed/known session the pre-spawn anchor is
                # used as-is; on a fresh session the first_user anchor (or
                # None, falling back to no filter) is. This is the fix for the
                # "first turn of an adopted session recaps nothing" gap — the
                # recap must see the sid/boundary that only adoption provides.
                if result is not None:
                    result = self._with_tool_recap(
                        result, anchor=turn_anchor
                    )
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
                        cmd += ["-c", self.session_name]
                        if hermes_chat_supports_flag(
                            self.hermes_bin, "--create-if-missing"
                        ):
                            cmd.append("--create-if-missing")
                    if model:
                        cmd += ["-m", model]
                    cmd.extend(self.extra_args)
                    with self._turn_lock:
                        # Fresh generation for the retry child — the hook
                        # must not be scoped to the dead first child.
                        self.bump_turn_alive_gen()
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
                        "skipping retry (would re-burn the window)",
                        getattr(self, "_last_run_ms", 0.0),
                    )
                    result = (
                        f"⏱️ Turn ran past the liveness window "
                        f"({self.liveness_timeout}s) — the model was still "
                        f"working but the turn took too long. Re-send to "
                        f"continue; or lower HERMES_LIVENESS_TIMEOUT."
                    )
            # Stop the step watcher now the child has finished (it only ever
            # pushes rows created during the turn). The `finally` below also
            # sets this on every error path (subprocess setup or turn
            # processing raising), so the daemon watcher is never orphaned
            # — an orphan would keep polling state.db and, because it uses
            # the client's current session + gen, refresh the liveness marker
            # for a *later* stalled turn, keeping it alive until the hard cap
            # and pushing its tool steps again. Event.set() is idempotent.
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

        finally:
            # Every exit path (normal, FileNotFoundError, any other
            # Exception) must stop the step watcher. See the comment above:
            # an orphaned daemon watcher refreshes the liveness marker for
            # subsequent turns and re-pushes their tool steps.
            watcher_stop.set()

    def _run_with_liveness_guard(
        self, cmd: list[str], anchor: tuple[str, int] | None = None
    ) -> str | None:
        """
        Run the hermes subprocess with a liveness guard.

        Kills the process if zero output (stdout+stderr) for
        ``liveness_timeout`` seconds. Returns the reply text or an
        error message.

        ``anchor`` is this turn's per-turn recap boundary (a local, threaded
        here — NOT shared state). It is NOT used to recap inside this method:
        the recap is applied by the caller AFTER session adoption, so the
        first-turn (adopted-session) anchor is already in place when the
        footer is built.
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
            # Real subprocess.PIPE file objects have .close(); test mocks may
            # pass plain iterators, which don't.
            if hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:  # noqa: BLE001 — cleanup must not raise
                    pass

        def _watchdog():
            if not self.liveness_timeout and not self.turn_hard_cap:
                return
            while not done.is_set():
                now = time.monotonic()
                # Hard cap: absolute wall clock on the whole turn, independent
                # of the silence window. Fires even if the marker is fresh (a
                # turn trickling tool rows for hours is still suspect).
                if self.turn_hard_cap and (now - started) > self.turn_hard_cap:
                    logger.warning(
                        "Liveness guard: turn exceeded the hard cap %ds, "
                        "killing hermes",
                        self.turn_hard_cap,
                    )
                    self._stop_requested = True
                    self._guard_killed = True
                    self._deny_veto = False
                    self._kill_process()
                    self.clear_turn_alive_marker()
                    return
                if not self.liveness_timeout:
                    done.wait(1.0)
                    continue
                with activity_lock:
                    remaining = (last_activity + self.liveness_timeout) - now
                if remaining <= 0:
                    logger.warning(
                        "Liveness guard: no output or heartbeat for %ds, "
                        "killing hermes",
                        self.liveness_timeout,
                    )
                    # Diagnostic: was this a scoping mismatch (marker session
                    # or gen not matching this child — e.g. on resume) vs a
                    # true stall? Logged once at the kill, best-effort.
                    self._log_marker_scoping_diag()
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
            # The recap is applied by chat() AFTER session adoption (the
            # first turn's anchor is only known during adoption), so return
            # the raw reply here and let the caller thread the per-turn
            # anchor in.
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

    def _with_tool_recap(
        self, reply: str, anchor: tuple[str, int] | None = None
    ) -> str:
        """Append a compact tool recap to the reply (recap fallback).

        Live tool steps are the primary channel; this is the safety net —
        if the live path is disabled or a hermes update changes the event
        surface, the mesh still sees which tools ran.

        Suppressed entirely in step mode: the CLI-side watcher already
        delivered each tool call + output as its own 💻 message, so a recap
        footer would be a redundant recap the user explicitly rejected.

        ``anchor`` is this turn's per-turn recap boundary, threaded in by the
        caller (chat() computes it under the turn lock and hands it over
        after adoption). Never reads shared client state.
        """
        if self._step_mode:
            _diag(f"  recap suppressed (step mode) reply={len(reply)} chars")
            return reply
        try:
            recap = self.tool_recap(limit=8, anchor=anchor)
        except Exception as exc:
            logger.debug("tool_recap failed: %s", exc)
            return reply
        if not recap:
            return reply
        names = [r["name"] for r in recap]
        from hermes_reticulum.core.tool_emoji import tool_emoji
        line = ", ".join(f"{tool_emoji(n)} {n}" for n in names)
        if len(recap) > 8:
            line += " …"
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
