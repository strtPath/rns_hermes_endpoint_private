"""Local HTTP control endpoint for the Hermes-Reticulum bridge."""

from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("hermes_reticulum.control_server")


DEFAULT_CONTROL_PORT = 8471
TOKEN_FILE_NAME = "control_token"
DEFAULT_APPROVAL_TIMEOUT = float(os.getenv("HERMES_MESH_APPROVAL_TIMEOUT", "900"))




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
    # session_name -> free-text deny reason (from /deny <reason>), if any
    _deny_reasons: dict[str, str] = field(default_factory=dict)
    # session_name -> steering text queued for the next turn
    steer_text: dict[str, str] = field(default_factory=dict)
    # session_name -> cumulative tool call count (session-scoped, never cleared)
    session_tool_count: dict[str, int] = field(default_factory=dict)


class ControlServer:
    """Threaded local HTTP server for tool-step events and control ops."""

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
        # Bounded so a flood can't grow memory unboundedly; drop + log when full.
        self._relay_q: "queue.Queue" = queue.Queue(maxsize=256)
        self._relay_worker: Optional[threading.Thread] = None
        # /status payload: monotonic start (uptime) + model id the bridge
        # serves (null until the CLI wires it through).
        self._started = time.monotonic()
        self._model: Optional[str] = None


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

    def answer_approval(
        self, session_name: str, approve: bool, reason: str = ""
    ) -> bool:
        """Resolve a pending gate. Returns True if a gate was pending.

        ``reason`` (an optional free-text from ``/deny <reason>``) is stored
        so the /gate/notify handler can relay it back to the plugin, which
        surfaces it in the BLOCKED message to the model.
        """
        st = self.state
        if session_name not in st._pending:
            return False
        st._decisions[session_name] = "approve" if approve else "deny"
        if not approve:
            st._deny_reasons[session_name] = reason
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
        st.session_tool_count[session_name] = st.session_tool_count.get(session_name, 0) + 1

    def clear_turn(self, session_name: str) -> None:
        self.state.turn_steps.pop(session_name, None)
        self.state.last_step.pop(session_name, None)

    def turn_recap(self, session_name: str) -> list[ToolStep]:
        return list(self.state.turn_steps.get(session_name, []))

    def session_tool_total(self, session_name: str) -> int:
        return self.state.session_tool_count.get(session_name, 0)

    def status_payload(self) -> dict[str, Any]:
        """Assemble the /status health dict for mesh self-diagnosis."""
        started = getattr(self, "_started", None)
        return {
            "model": self._model,
            "session": {
                "running": bool(
                    self.state.turn_steps
                    or self.state._pending
                    or self.state.steer_text
                ),
                "active_sessions": sorted(
                    set(self.state.turn_steps)
                    | set(self.state._pending)
                    | set(self.state.steer_text)
                ),
                "pending_approvals": sorted(self.state._pending.keys()),
            },
            "uptime": (
                round(time.monotonic() - started, 3)
                if started is not None else 0.0
            ),
            "acl": self._acl_mode(),
        }

    def _acl_mode(self) -> str:
        allow_all = os.getenv("HERMES_RETICUM_ALLOW_ALL", "true").lower()
        if allow_all in ("true", "1", "yes"):
            return "open"
        allowed_raw = os.getenv("HERMES_RETICUM_ALLOWED_USERS", "").strip()
        return "allowlist" if allowed_raw else "closed"

    def _handle_post(self, path: str, body: dict, token_ok: bool) -> tuple[int, str]:
        if not token_ok:
            return 401, "unauthorized"

        if path == "/step":
            session = body.get("session", "")
            if not session:
                return 400, "missing session"
            # Hook only POSTs risky steps for the gate; safe steps come as kind='report'.
            kind = body.get("kind", "gate")
            name = body.get("tool", "tool")
            args = body.get("args")
            result = body.get("result")
            is_error = bool(body.get("is_error", False))
            step = ToolStep(name=name, args=args, result=result,
                            is_error=is_error)
            self.record_step(session, step)
            if kind == "gate":
                # Offload the mesh push; approval wait stays inline (hook blocks on it).
                if self.on_gate_open is not None:
                    # Mesh push → offload; command/description empty for ack gate.
                    self.relay(self.on_gate_open, session, name, "", "")
                decision = self.request_approval(session, name)
                if decision == "deny" and self.on_deny is not None:
                    # Veto kills the hermes child; offload so a bad callback can't block.
                    self.relay(self.on_deny, session)
                return 200, decision
            # kind == "report": fire-and-forget mesh push; hook doesn't block on reply.
            if self.on_step is not None:
                self.relay(self.on_step, session, step)
            return 200, "ok"

        if path == "/step/full":
            # Step-through: chunk full tool output into multiple LXMF posts.
            session = body.get("session", "")
            text = body.get("body", "")
            if not session or not text:
                return 400, "missing session/body"
            if self.on_full_step is not None:
                # Most blocking path (32KB ≈ 21 posts × 0.5s) → offload entirely.
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
            # Pre-exec gate from mesh-tool-gate plugin. Command already confirmed
            # dangerous by Hermes' detect_dangerous_command. Returns verdict in
            # HTTP response (plugin blocks on it). Distinct from /step kind='gate':
            # no on_deny here — we refuse the tool, model sees the reason.
            session = body.get("session", "")
            if not session:
                return 400, "missing session"
            name = body.get("tool", "terminal")
            command = body.get("command", "")
            description = body.get("description", "")
            if self.on_gate_open is not None:
                # (session, tool, command, description) — real values from pre-exec path.
                self.relay(
                    self.on_gate_open, session, name, command, description
                )
            decision = self.request_approval(
                session, name,
                on_wait=lambda: None,
            )
            # Restore the pre-Fix-4 behavior: a deny does NOT kill the turn.
            # Instead we refuse the tool and return the verdict (plus the
            # operator's /deny reason, when given) so the plugin can block
            # with the gateway-aligned message and let the model continue.
            deny_reason = self.state._deny_reasons.pop(session, "")
            if decision == "deny" and deny_reason:
                return 200, json.dumps({"verdict": "deny", "reason": deny_reason})
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

        def _send_json(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, default=str).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, "ok" if ctrl.running else "down")
                return
            if parsed.path == "/status":
                if not self._token_ok():
                    self._send(401, "unauthorized")
                    return
                self._send_json(200, ctrl.status_payload())
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
