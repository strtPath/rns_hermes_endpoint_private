"""Local HTTP control endpoint for the Hermes-Reticulum bridge.

Bridges the two sides of the system:

  * Hermes Agent hooks (``agent:step``, running inside the gateway process)
    POST tool-step events here.
  * The mesh (via slash commands handled by :mod:`commands`) drives
    ``approve`` / ``deny`` / ``stop`` / ``steer`` here.

Security model
--------------
- Binds to 127.0.0.1 only (configurable, never expose it).
- Every request must carry the shared token, either as
  ``?token=...`` (query) or the ``X-Hermes-Token`` header.
- The token is generated at bridge startup and written to a 0600 file
  under the Reticulum storage dir; the hook reads it from there.

Approval gate
-------------
:func:`request_approval` blocks (in the hook's thread) until the operator
answers via ``/approve`` or ``/deny`` on the mesh (or the timeout elapses).
The hook calls it only for *risky* tool steps, so non-risky tools never
add latency.

The server runs on its own thread with a daemon thread-pool; it never
blocks the bridge's message thread.
"""

from __future__ import annotations

import logging
import os
import queue
import secrets
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("hermes_reticulum.control_server")

# Default port for the local control endpoint (override via env/args).
DEFAULT_CONTROL_PORT = 8471
TOKEN_FILE_NAME = "control_token"
DEFAULT_APPROVAL_TIMEOUT = 120.0  # seconds to wait for /approve or /deny


# ──────────────────────────────────────────────────────────────────────
# Risk classification (deny-by-default gate for risky tools)
# ──────────────────────────────────────────────────────────────────────

# Tools that are read-only or harmless — they never gate.
SAFE_TOOLS = {
    "read_file", "search_files", "web_search", "web_extract",
    "fact_store", "fact_feedback", "session_search", "skills_list",
    "skill_view", "clarify", "todo", "page_info", "vision_analyze",
}

# Tools that mutate state or shell out — they require /approve by default.
RISKY_TOOLS = {
    "terminal", "write_file", "patch", "memory", "cronjob",
    "delegate_task", "text_to_speech",
}


@dataclass
class ToolStep:
    """One tool execution reported by an ``agent:step`` hook event."""

    name: str
    args: Any = None
    result: Optional[str] = None
    is_error: bool = False

    def summary(self) -> str:
        """Compact one-line summary for mesh display (bandwidth-aware)."""
        base = f"🔧 {self.name}"
        if self.is_error:
            base += " ❌"
        return base


def classify_tool(name: str) -> str:
    """Return ``'safe'`` | ``'risky'`` | ``'unknown'`` for a tool name.

    Unknown tools gate (deny-by-default) so new/dangerous toolsets are
    surfaced to the operator rather than silently executed.
    """
    if name in SAFE_TOOLS:
        return "safe"
    if name in RISKY_TOOLS or name.startswith(("browser_", "execute_")):
        return "risky"
    return "unknown"


# ──────────────────────────────────────────────────────────────────────
# Control server
# ──────────────────────────────────────────────────────────────────────

@dataclass
class ControlState:
    """Mutable per-turn state shared between HTTP handler and commands."""

    token: str = ""
    # session_name -> latest reported step (for /status-style inspection)
    last_step: dict[str, ToolStep] = field(default_factory=dict)
    # session_name -> list of ToolSteps this turn (for /tools recap)
    turn_steps: dict[str, list[ToolStep]] = field(default_factory=dict)
    # session_name -> pending approval event
    _pending: dict[str, threading.Event] = field(default_factory=dict)
    # session_name -> decision for pending approval ("approve" | "deny")
    _decisions: dict[str, str] = field(default_factory=dict)
    # session_name -> steering text queued for the next turn
    steer_text: dict[str, str] = field(default_factory=dict)


class ControlServer:
    """Threaded local HTTP server for tool-step events and control ops.

    Usage::

        ctrl = ControlServer(token="...", storage_path="~/.lxmf/storage")
        ctrl.start()
        ...
        ctrl.stop()

    The hook POSTs to ``/step``; mesh commands call
    :meth:`answer_approval` / :meth:`queue_steer` directly (same process),
    or POST to ``/approve`` / ``/deny`` / ``/steer`` (for external drivers).
    """

    def __init__(
        self,
        port: int = DEFAULT_CONTROL_PORT,
        host: str = "127.0.0.1",
        token: Optional[str] = None,
        storage_path: Optional[str] = None,
        approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT,
    ):
        self.port = port
        self.host = host
        self.approval_timeout = approval_timeout
        self.state = ControlState()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # Wired by the CLI: called with the mesh session name when an
        # approval gate is denied (abort the in-flight hermes child).
        self.on_deny: Optional[Callable[[str], None]] = None
        # Wired by the CLI: called with (mesh session, ToolStep) for every
        # step the hook reports — the bridge uses this to push a live
        # "🔧 tool" message to the mesh peer.
        self.on_step: Optional[Callable[[str, Any], None]] = None
        # Wired by the CLI: called with (mesh session, full_text) for
        # step-through mode — the bridge chunks this into multiple LXMF
        # posts (user-approved bandwidth cost).
        self.on_full_step: Optional[Callable[[str, str], None]] = None
        # Wired by the CLI: called when a gate opens, so the bridge can
        # push a "⏸️ waiting for /approve" message to the mesh.
        # Signature: (session, tool, command, description). The /step ack
        # gate passes (session, label, "", ""); /gate/notify (pre-exec)
        # passes the real tool/command/description.
        self.on_gate_open: Optional[Callable[[str, str, str, str], None]] = None
        self._token = token or secrets.token_urlsafe(32)
        self._token_path: Optional[Path] = None
        if storage_path:
            self._persist_token(storage_path)
        # Bounded work queue for *mesh-bound* side effects (tool pushes,
        # full-step chunks, "gate opened" notices). These are fired from the
        # HTTP handler thread; if a mesh peer is offline/slow they can block
        # for a long time, which would wedge the request thread — and, for
        # the hook's blocking gate POST, the *gateway* event loop that called
        # it. So we enqueue and return immediately; a single dedicated
        # worker drains the queue (FIFO → preserves message order), and the
        # approval *wait* (request_approval's event.wait) is NOT moved here —
        # it stays inline because it's the only thing the hook's gate must
        # actually block on.
        # Bounded (256) so a flood can't grow memory unboundedly; when full we
        # drop + log (a lost "🔧 tool" push is cosmetic, a wedged bridge is not).
        self._relay_q: "queue.Queue" = queue.Queue(maxsize=256)
        self._relay_worker: Optional[threading.Thread] = None

    # ── token ────────────────────────────────────────────────────────

    def _persist_token(self, storage_path: str) -> None:
        base = Path(storage_path).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        self._token_path = base / TOKEN_FILE_NAME
        try:
            self._token_path.write_text(self._token, encoding="utf-8")
            os.chmod(self._token_path, 0o600)
        except OSError as e:
            logger.warning("Could not persist control token: %s", e)

    @property
    def token_path(self) -> Optional[Path]:
        return self._token_path

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> bool:
        if self._thread is not None:
            return False
        handler = _make_handler(self)
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as e:
            logger.error("Control server failed to bind %s:%s: %s",
                         self.host, self.port, e)
            return False
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="control-server",
            daemon=True,
        )
        self._thread.start()
        self._start_relay_worker()
        logger.info(
            "Control endpoint listening on %s:%s (token file: %s)",
            self.host, self.port, self._token_path or "(none)",
        )
        return True

    def stop(self) -> None:
        self._stop_relay_worker()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Control endpoint stopped")

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ── mesh relay queue (offload blocking mesh sends) ───────────────

    def _start_relay_worker(self) -> None:
        if self._relay_worker is not None:
            return
        self._relay_worker = threading.Thread(
            target=self._relay_loop, name="control-relay", daemon=True
        )
        self._relay_worker.start()

    def _stop_relay_worker(self) -> None:
        w = self._relay_worker
        if w is None:
            return
        self._relay_worker = None
        # Sentinel: poison-pill to unblock a waiting worker.
        try:
            self._relay_q.put_nowait(None)
        except queue.Full:
            pass
        w.join(timeout=5)

    def _relay_loop(self) -> None:
        """Drain mesh-bound side effects one at a time (order-preserving)."""
        while True:
            item = self._relay_q.get()
            if item is None:  # shutdown sentinel
                return
            fn, args = item
            try:
                fn(*args)
            except Exception:
                logger.debug("mesh relay failed", exc_info=True)

    def relay(self, fn: Callable, *args) -> None:
        """Schedule a blocking mesh-bound callback off the request thread.

        Never raises. If the bounded queue is full the task is dropped with a
        warning (cosmetic loss, never a wedge).
        """
        try:
            self._relay_q.put_nowait((fn, args))
        except queue.Full:
            logger.warning(
                "mesh relay queue full — dropping callback %s",
                getattr(fn, "__name__", fn),
            )

    # ── approval gate API (used by the hook via POST /step, and by
    # mesh commands in-process) ───────────────────────────────────────

    def request_approval(
        self,
        session_name: str,
        tool_name: str,
        timeout: Optional[float] = None,
        on_wait: Optional[Callable[[], None]] = None,
    ) -> str:
        """Block until the operator approves or denies a risky tool.

        Returns ``'approve'`` or ``'deny'``. On timeout the *safer* default
        is to deny (deny-by-default) so an unanswered gate never executes
        a risky tool.

        ``on_wait`` is called once when the gate is opened (used by the
        hook to push a "⏸️ waiting for /approve" message to the mesh).
        """
        timeout = self.approval_timeout if timeout is None else timeout
        st = self.state
        event = threading.Event()
        st._pending[session_name] = event
        st._decisions.pop(session_name, None)
        if on_wait is not None:
            try:
                on_wait()
            except Exception:
                logger.debug("on_wait callback failed", exc_info=True)
        got = event.wait(timeout)
        st._pending.pop(session_name, None)
        decision = st._decisions.pop(session_name, None)
        if not got:
            return "deny"  # timeout → deny-by-default
        return decision or "deny"

    def answer_approval(self, session_name: str, approve: bool) -> bool:
        """Resolve a pending gate. Returns True if a gate was pending."""
        st = self.state
        if session_name not in st._pending:
            return False
        st._decisions[session_name] = "approve" if approve else "deny"
        st._pending[session_name].set()
        return True

    def has_pending_approval(self, session_name: str) -> bool:
        return session_name in self.state._pending

    def queue_steer(self, session_name: str, text: str) -> None:
        self.state.steer_text[session_name] = text

    def pop_steer(self, session_name: str) -> Optional[str]:
        return self.state.steer_text.pop(session_name, None)

    def record_step(self, session_name: str, step: ToolStep) -> None:
        st = self.state
        st.last_step[session_name] = step
        st.turn_steps.setdefault(session_name, []).append(step)

    def clear_turn(self, session_name: str) -> None:
        self.state.turn_steps.pop(session_name, None)
        self.state.last_step.pop(session_name, None)

    def turn_recap(self, session_name: str) -> list[ToolStep]:
        return list(self.state.turn_steps.get(session_name, []))

    # ── HTTP plumbing ────────────────────────────────────────────────

    def _handle_post(self, path: str, body: dict, token_ok: bool) -> tuple[int, str]:
        st = self.state
        if not token_ok:
            return 401, "unauthorized"

        if path == "/step":
            session = body.get("session", "")
            if not session:
                return 400, "missing session"
            # The hook already decided this tool is risky (it only POSTs
            # risky steps for the gate; safe steps come as kind='report').
            kind = body.get("kind", "gate")
            name = body.get("tool", "tool")
            args = body.get("args")
            result = body.get("result")
            is_error = bool(body.get("is_error", False))
            step = ToolStep(name=name, args=args, result=result,
                            is_error=is_error)
            self.record_step(session, step)
            if kind == "gate":
                # Block until /approve or /deny (or timeout). On deny the
                # operator wants this turn aborted, so the bridge kills
                # its in-flight hermes child immediately.
                if self.on_gate_open is not None:
                    # "gate opened" is a mesh push (blocking LXMF send) →
                    # offload it so it can't wedge the request thread while
                    # the approval wait below runs inline. Pass the tool
                    # label; command/description stay empty for the ack
                    # gate (the /gate/notify pre-exec path fills them).
                    self.relay(self.on_gate_open, session, name, "", "")
                decision = self.request_approval(session, name)
                if decision == "deny" and self.on_deny is not None:
                    # on_deny vetoes the turn (kills the hermes child) —
                    # fast and in-process, safe inline; still offload so a
                    # misbehaving callback can't block the reply.
                    self.relay(self.on_deny, session)
                return 200, decision
            # kind == "report": fire the live "💻 tool" mesh push OFF the
            # request thread. The hook POSTs this and does NOT block on its
            # reply, so returning immediately is safe and keeps the gateway
            # loop (for the hook) unblocked.
            if self.on_step is not None:
                self.relay(self.on_step, session, step)
            return 200, "ok"

        if path == "/step/full":
            # Step-through mode: the hook POSTs the full tool call + full
            # output as a pre-formatted text body.  We chunk it into
            # multiple LXMF posts (user-approved bandwidth cost).
            session = body.get("session", "")
            text = body.get("body", "")
            if not session or not text:
                return 400, "missing session/body"
            if self.on_full_step is not None:
                # Chunking + multiple LXMF sends is the *most* blocking path
                # (a 32KB step is ~21 posts × 0.5s). Offload it entirely.
                self.relay(self.on_full_step, session, text)
            else:
                logger.warning(
                    "on_full_step not wired — dropping full step for %s",
                    session,
                )
            return 200, "ok"

        if path in ("/approve", "/deny"):
            session = body.get("session", "")
            ok = self.answer_approval(session, path == "/approve")
            return (200, "ok") if ok else (409, "no pending approval")

        if path == "/gate/notify":
            # Pre-execution gate from the in-process pre_tool_call plugin
            # (mesh-tool-gate). The plugin has ALREADY run Hermes' own
            # detect_dangerous_command, so by the time we're here the command
            # is confirmed dangerous. We open an approval gate for this
            # session, push the command to the mesh operator, and RETURN the
            # verdict in the HTTP response (the plugin then blocks on the
            # result). Reuses the existing /approve //deny resolution + on_gate
            # push, so the operator's UX is identical to the ack-then-veto
            # gate. Distinct from /step's kind='gate' (that path also fires
            # on_deny to kill the hermes child — wrong for a pre-exec block,
            # where we just refuse the tool and the model sees the reason).
            session = body.get("session", "")
            if not session:
                return 400, "missing session"
            name = body.get("tool", "terminal")
            command = body.get("command", "")
            description = body.get("description", "")
            if self.on_gate_open is not None:
                # Signature: (session, tool, command, description).
                self.relay(
                    self.on_gate_open, session, name, command, description
                )
            decision = self.request_approval(
                session, name,
                on_wait=lambda: None,
            )
            return 200, decision

        if path == "/stop":
            return 200, "ok"  # no-op; stop is handled in-process

        if path == "/steer":
            session = body.get("session", "")
            text = body.get("text", "")
            if not session or not text:
                return 400, "missing session/text"
            self.queue_steer(session, text)
            return 200, "ok"

        if path == "/status":
            return 200, (
                f"running; pending={list(self.state._pending.keys())}"
            )

        if path == "/clear_turn":
            session = body.get("session", "")
            self.clear_turn(session)
            return 200, "ok"

        return 404, "not found"


def _make_handler(server: ControlServer):
    ctrl = server

    class Handler(BaseHTTPRequestHandler):
        def _token_ok(self) -> bool:
            # Header or query param; constant-time compare.
            tok = self.headers.get("X-Hermes-Token", "")
            if not tok:
                q = parse_qs(urlparse(self.path).query).get("token", [""])
                tok = q[0]
            return secrets.compare_digest(tok, ctrl.state.token or ctrl._token)

        def _send(self, code: int, body: str) -> None:
            data = body.encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError) as exc:
                # The peer (typically the Hermes gateway) restarted mid-response
                # and dropped the TCP connection. This is expected during a
                # gateway restart cycle and is not actionable — log quietly
                # instead of dumping a full traceback to journald.
                logger.debug(
                    "control-server: dropped reply to %s (code=%d, %d bytes): %s",
                    self.client_address[0], code, len(data), exc,
                )
                self.close_connection = True

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, "ok" if ctrl.running else "down")
                return
            self._send(404, "not found")

        def do_POST(self):  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                import json
                body = json.loads(raw or b"{}")
            except Exception:
                self._send(400, "bad json")
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            code, body_out = ctrl._handle_post(path, body, self._token_ok())
            self._send(code, body_out)

        def log_message(self, fmt, *args):  # silence default logging
            logger.debug("control-server: " + fmt, *args)

    return Handler
