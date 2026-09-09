"""mesh-tool-gate — pre-execution approve/deny gate for the Reticulum mesh.

Runs in-process on the ``pre_tool_call`` hook, which fires in
``agent/tool_executor.py::_authorized_dispatch`` BEFORE a tool dispatches.
So unlike the acknowledge-then-veto ``agent:step`` hook, this gate stops a
tool *before* it runs.

Detection is NOT duplicated here. It reuses Hermes' own single source of
truth in ``tools/approval.py``:

* :func:`detect_hardline_command` — no-recovery commands (rm -rf /, mkfs,
  dd to raw device, shutdown/reboot, fork bomb, kill -1). Blocked OUTRIGHT:
  we return ``{"action": "block"}`` so the tool never runs and the model sees
  the reason. This mirrors Hermes' hardline floor (which fires even under
  yolo).

* :func:`detect_dangerous_command` — the full DANGEROUS_PATTERNS table
  (force-push, sudo, chmod 777, curl|sh, sensitive-path writes, ...).
  Gated via the bridge's mesh control server.

* For file-writing tools (write_file, patch), we build a synthetic shell
  command from the target path and run it through the SAME
  ``detect_dangerous_command`` that catches sensitive-path writes
  (e.g. ``>> ~/.ssh/authorized_keys``, ``tee /etc/...``, overwriting
  ``~/.hermes/.env``). A ``write_file`` to ``~/.ssh/authorized_keys`` is
  detected exactly like ``echo x >> ~/.ssh/authorized_keys``.

* For ``execute_code``, we treat it as risky (the Python payload is opaque
  to regex detection), so it gates unconditionally on the mesh.

* No gate for safe tools (read_file, search_files, web_search, etc.) —
  they proceed freely. Their output is still streamed live to the mesh by
  the separate ``mesh-tool-events`` hook on ``agent:step``.

Architecture note — why we run our own gate instead of returning
``{"action": "approve"}`` to Hermes: the bridge runs ``hermes chat -q``
children (CLI mode), and Hermes' approval gate falls through to
``prompt_dangerous_approval()`` which calls ``input()`` — which hangs
forever in quiet mode.  The mesh gate must therefore reach out to the
bridge's control server directly, push the prompt to the mesh operator
over LXMF, and block until the operator answers.

Session-name resolution (the fix this card implements) — the control
server keys every approval by the bridge's *mesh thread name*
(``sessions.title`` in state.db, e.g. ``mesh-reticulum-1788627270``), not
by Hermes' own session key.  This plugin used to POST
``get_current_session_key()`` (e.g. ``agent:default:...``), which the
control server could never match, so:

* the "⚠️ PRE-EXEC APPROVAL" push silently dropped (no mesh peer for that
  session key), and
* ``/approve`` targeted the bridge thread name, not the plugin's session
  key → the gate never resolved → the 900s deny-by-default fired.

Fixes here: we resolve the ``session_id`` → ``sessions.title`` via
``state.db`` and pass THAT (the mesh thread name) as the ``session`` on
the ``/gate/notify`` POST.  ``_is_mesh_session()`` is also now scoped to
``mesh-*`` titles only, so non-mesh (operator-owned) sessions are never
gated.

Configuration (env, read at import):
  HERMES_MESH_CONTROL_URL   default http://127.0.0.1:8471
  HERMES_MESH_TOKEN_FILE    default ~/.lxmf/storage/control_token
  HERMES_MESH_PROFILE       optional; only gate turns for this profile prefix
  MESH_GATE_TIMEOUT         seconds the operator has to answer; on timeout the
                            Hermes approval gate fails closed (blocks).
                            default 900
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.request

logger = logging.getLogger("mesh-tool-gate")

CONTROL_URL = os.environ.get("HERMES_MESH_CONTROL_URL", "http://127.0.0.1:8471")
TOKEN_FILE = os.environ.get(
    "HERMES_MESH_TOKEN_FILE",
    os.path.expanduser("~/.lxmf/storage/control_token"),
)
PROFILE_FILTER = os.environ.get("HERMES_MESH_PROFILE", "").strip()

# Hermes' canonical session store. We query it (read-only) to map the
# plugin's session_id → the bridge's mesh thread name (sessions.title),
# which is the key the control server uses for every approval.
STATE_DB = os.environ.get(
    "HERMES_STATE_DB", os.path.expanduser("~/.hermes/state.db")
)

# Bounded timeout — this hook runs inside the gateway's asyncio event loop and
# MUST NOT park it. The gate wait is the operator's window to answer; on
# timeout the control server denies (fail-closed), and we surface that as a
# block. The "block on timeout" guarantee is implemented fail-closed at both
# layers.
_GATE_TIMEOUT = float(os.environ.get("MESH_GATE_TIMEOUT", "900"))

# ── Tool scope: which tools we inspect ─────────────────────────────────
# terminal:        shell commands → run through Hermes' detector directly
# write_file:      target path → build synthetic shell redirect + detect
# patch:           target path → same as write_file
# execute_code:    Python payload is opaque → always gate (risky)
# All other tools: proceed ungated (their output is streamed by agent:step)
_GATED_TOOLS = {"terminal", "write_file", "patch", "execute_code"}


# ---------------------------------------------------------------------------
# Mesh plumbing
# ---------------------------------------------------------------------------

def _token() -> str | None:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _gate_tool(
    session: str,
    tool_name: str,
    command: str,
    description: str,
) -> tuple[bool, str, str]:
    """Open a pre-exec approval gate on the mesh control server and wait for
    the operator's verdict.

    ``session`` is the bridge's mesh thread name (``sessions.title``), which
    is the key the control server uses to route ``/approve`` / ``/deny`` and
    look up the mesh peer for the push.

    Returns ``(decision == 'approve', verdict_text, deny_reason)``.

    FAIL-CLOSED: any error (no control server, timeout, bad token, no
    decision) → ``(False, verdict, '')``. An unanswered gate never becomes
    a silently-executed dangerous command.

    When the operator denies with an explicit reason (``/deny <reason>``),
    the control server returns a JSON body ``{"verdict": "deny", "reason":
    "<reason>"}``; ``deny_reason`` is parsed out so the caller can surface
    it in the BLOCKED message.
    """
    url = f"{CONTROL_URL}/gate/notify"
    tok = _token()
    if not tok:
        logger.warning("mesh-tool-gate: no control token — failing closed")
        return False, "no control endpoint", ""
    req = urllib.request.Request(
        url,
        data=json.dumps({
            "session": session,
            "tool": tool_name,
            "command": command,
            "description": description,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Hermes-Token": tok},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_GATE_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001 — fail closed on any failure
        logger.warning("mesh-tool-gate: gate notify failed (fail-closed): %s", e)
        return False, "gate-error", ""
    # Newer control server returns JSON when a deny reason is present.
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            verdict = obj.get("verdict", raw)
            deny_reason = obj.get("reason", "")
        except (ValueError, TypeError):
            verdict, deny_reason = raw, ""
    else:
        verdict, deny_reason = raw, ""
    return verdict == "approve", verdict, deny_reason


def _resolve_mesh_session(session_id: str) -> str | None:
    """Map a Hermes session id → the bridge's mesh thread name.

    The bridge spawns ``hermes chat -c <title>``; the child's session is
    stored in state.db with ``title`` = the mesh thread name and ``id`` =
    the concrete session id.  Returns the title, or None if this session
    isn't a mesh session.
    """
    if not session_id:
        return None
    try:
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    except sqlite3.Error as e:
        logger.warning("mesh-tool-gate: state.db open failed: %s", e)
        return None
    try:
        row = conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    except sqlite3.Error as e:
        logger.warning("mesh-tool-gate: state.db query failed: %s", e)
        return None
    finally:
        conn.close()
    return row[0] if row else None


def _is_mesh_session(session_id: str = "") -> bool:
    """True when this turn is a mesh (bridge) session.

    Primary guard: resolve ``session_id`` → ``sessions.title`` and require
    it to start with ``mesh-``.  Non-mesh (operator-owned) sessions are
    never gated — this is what fixes the "broke hermes" bug where an unset
    ``HERMES_MESH_PROFILE`` gated EVERY session.

    ``PROFILE_FILTER`` remains a secondary guard for multi-profile setups
    (optional), applied only after the ``mesh-`` prefix passes.
    """
    title = _resolve_mesh_session(session_id)
    if not title or not title.startswith("mesh-"):
        return False
    if PROFILE_FILTER and not title.startswith(PROFILE_FILTER):
        return False
    return True


# ---------------------------------------------------------------------------
# Detection — reuse Hermes' own single source of truth
# ---------------------------------------------------------------------------

def _extract_command(tool_name: str, args) -> str | None:
    """Pull the shell-command or synthetic-path string from tool args.

    For ``terminal``:    extract ``command`` directly.
    For ``write_file``:  build ``>> <target_path>`` for pattern matching.
    For ``patch``:       build ``>> <target_path>`` (same sensitive-path check).
    For ``execute_code``: return a placeholder — the Python payload is opaque.
    """
    if not isinstance(args, dict):
        return None
    if tool_name == "terminal":
        cmd = args.get("command")
        return cmd if isinstance(cmd, str) and cmd.strip() else None
    if tool_name in ("write_file", "patch"):
        path = args.get("path")
        if isinstance(path, str) and path.strip():
            # Build a synthetic append-redirect that Hermes' sensitive-path
            # patterns will catch (e.g. ``>> ~/.ssh/authorized_keys``,
            # ``>> ~/.hermes/.env``, ``tee /etc/...``).
            return f">> {path.strip()}"
        return None
    if tool_name == "execute_code":
        # Opaque Python payload — always treat as risky.
        # Return a placeholder so the gate fires.
        return "<execute_code — opaque Python payload>"
    return None


def _build_description(tool_name: str, args) -> str:
    """Build a human-readable description for the gate prompt."""
    if tool_name == "terminal":
        if isinstance(args, dict):
            cmd = args.get("command", "")
            if isinstance(cmd, str) and cmd.strip():
                return cmd.strip()
        return "terminal command"
    if tool_name == "write_file":
        if isinstance(args, dict):
            path = args.get("path", "")
            if isinstance(path, str) and path.strip():
                return f"write_file → {path.strip()}"
        return "write_file"
    if tool_name == "patch":
        if isinstance(args, dict):
            path = args.get("path", "")
            if isinstance(path, str) and path.strip():
                return f"patch → {path.strip()}"
        return "patch"
    if tool_name == "execute_code":
        if isinstance(args, dict):
            code = args.get("code", "")
            if isinstance(code, str) and code.strip():
                # First line is usually the most descriptive
                first_line = code.strip().split("\n")[0][:120]
                return f"execute_code: {first_line}"
        return "execute_code"
    return tool_name


# ── Block message builder — mirrors the gateway BLOCKED format ───────
# See agent/tools/approval.py:3965-3971. The gateway does NOT kill the turn
# on /deny: it blocks the tool and returns this exact message so the model
# can adapt. We reuse the identical wording so the mesh operator sees the
# same instruction whether the denial was explicit or a timeout.

def _block_message(
    verdict: str,
    deny_reason: str = "",
) -> str:
    """Build the gateway-aligned ``BLOCKED`` message for a denied gate.

    - Explicit deny (verdict == 'deny'): "Action denied by user.", plus the
      reason clause when the operator gave one.
    - Timeout / failure (anything else): the timeout wording, so the model
      gets the same "do NOT retry/rephrase" guardrail without the fake
      operator attribution; silence is not consent.
    """
    if verdict == "deny":
        reason_addendum = (
            f' Reason given by the user: "{deny_reason}".' if deny_reason else ""
        )
        return (
            f"BLOCKED: Action denied by user.{reason_addendum} The user "
            "has NOT consented to this action. Do NOT retry it, do NOT "
            "rephrase it, and do NOT attempt the same outcome via a "
            "different path."
        )
    # timeout / gate-error / no-control-endpoint → gateway timeout wording
    return (
        "BLOCKED: Action timed out without user response. The user has "
        "NOT consented to this action. Do NOT retry it, do NOT rephrase "
        "it, and do NOT attempt the same outcome via a different path. "
        "Silence is not consent."
    )


# ---------------------------------------------------------------------------
# pre_tool_call handler
# ---------------------------------------------------------------------------

def on_pre_tool_call(
    tool_name: str = "",
    args: dict | None = None,
    session_id: str = "",
    **_: object,
) -> dict | None:
    """pre_tool_call directive for the mesh bridge.

    Returns a directive dict (block) or None (proceed).
    """
    if tool_name not in _GATED_TOOLS:
        return None
    if not _is_mesh_session(session_id):
        return None

    # Resolve the bridge's mesh thread name ONCE — this is the session key
    # the control server uses for the push and for /approve / /deny.
    mesh = _resolve_mesh_session(session_id)
    if not mesh:
        return None

    # --- YOLO check: skip all gating when the user accepts the risk ---
    try:
        if os.environ.get("HERMES_YOLO_MODE") in ("1", "true", "yes", "on"):
            return None
    except Exception:  # noqa: BLE001
        pass

    # --- execute_code: always gate (opaque Python payload) ---
    if tool_name == "execute_code":
        desc = _build_description(tool_name, args)
        approved, verdict, deny_reason = _gate_tool(mesh, tool_name, desc, "execute_code")
        if not approved:
            return {
                "action": "block",
                "message": _block_message(verdict, deny_reason),
            }
        return None  # approved → proceed

    # --- terminal / write_file / patch: classify via Hermes' detector ---
    command = _extract_command(tool_name, args)
    if not command:
        return None

    # Hardline floor: no-recovery commands. Block outright (the tool never
    # runs). We do NOT consult yolo here — the hardline floor sits below
    # yolo, and for a mesh control surface "no recovery" should always stop.
    try:
        from tools.approval import detect_hardline_command
        is_hardline, hardline_desc = detect_hardline_command(command)
    except Exception as e:  # noqa: BLE001
        logger.warning("mesh-tool-gate: hardline detect failed: %s", e)
        is_hardline, hardline_desc = False, ""
    if is_hardline:
        logger.warning(
            "mesh-tool-gate: HARDLINE BLOCK %s %r (%s)",
            tool_name, command[:120], hardline_desc,
        )
        return {
            "action": "block",
            "message": (
                f"BLOCKED (hardline, no recovery): {hardline_desc}\n"
                f"{_build_description(tool_name, args)}\n"
                "This action is refused outright — it has no recovery path. "
                "Choose a safer approach."
            ),
        }

    # Dangerous check — reuse Hermes' exact detection so the set of gated
    # actions is identical to what Hermes already gates by default.
    try:
        from tools.approval import detect_dangerous_command
        is_dangerous, pattern_key, description = detect_dangerous_command(command)
    except Exception as e:  # noqa: BLE001
        logger.warning("mesh-tool-gate: dangerous detect failed: %s", e)
        # Fail-safe: if detection errors, escalate to the human gate.
        is_dangerous, pattern_key, description = True, "", "detection error"
    if not is_dangerous:
        return None  # safe → proceed (output still streamed by agent:step)

    # Dangerous — open the pre-exec approval gate on the mesh.
    desc = _build_description(tool_name, args)
    approved, verdict, deny_reason = _gate_tool(
        mesh,
        tool_name,
        desc,
        description or pattern_key or tool_name,
    )

    if not approved:
        reason = deny_reason or verdict
        logger.warning(
            "mesh-tool-gate: DANGEROUS BLOCKED %s %r (%s; %s)",
            tool_name, command[:120], description or pattern_key, reason,
        )
        return {
            "action": "block",
            "message": _block_message(verdict, deny_reason),
        }

    # Operator approved — proceed with the tool.
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)