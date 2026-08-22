"""
Slash-command dispatcher for the mesh bridge.

This is the extensible home for all `/` control commands the endpoint
understands (e.g. `/model`, `/new`, `/help`). Each command is a small
function: ``(ctx, args) -> str | None``.

- If the function returns a **string**, that string is sent back to the
  sender and the message is NOT forwarded to Hermes.
- If it returns **None**, the dispatcher treats it as "not a command" and
  the message falls through to the normal model path.

To add a new command, see the ``COMMANDS`` dict below — add a ``"/cmd":
handler`` entry and it's live after a bridge restart. No other code changes
required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from hermes_reticulum.core.hermes_client import HermesClient
from hermes_reticulum.core.model_command import ModelCommandHandler
from hermes_reticulum.core.control_server import ControlServer

logger = logging.getLogger("hermes_reticulum.commands")


@dataclass
class CommandContext:
    """Per-bridge state handed to every command handler."""

    hermes: HermesClient
    model_handler: ModelCommandHandler
    # ControlServer instance (None until the CLI wires it).
    control_server: ControlServer | None = None
    # Free-form bag for handlers that need to stash cross-call state.
    state: dict = field(default_factory=dict)


CommandFn = Callable[[CommandContext, str], str | None]


# ──────────────────────────────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────────────────────────────


def _cmd_stop(ctx: CommandContext, args: str) -> str | None:
    killed = ctx.hermes.stop()
    if killed:
        return "⛔ Stopped the running Hermes process."
    return "Nothing to stop — no active process."


def _cmd_status(ctx: CommandContext, args: str) -> str | None:
    h = ctx.hermes
    lines = [
        "📊 Bridge status",
        f"  Model:      {h.get_model()}",
        f"  Session:    {h.session_name}" + (f" ({h._resume_id[:12]}…)" if h._resume_id else " (unpinned)"),
        f"  Running:    {'yes' if h.is_running() else 'no'}",
        f"  Hard cap:   {h.timeout}s",
        f"  Liveness:   {h.liveness_timeout}s" + (" (off)" if not h.liveness_timeout else ""),
    ]
    total_tokens, msg_count, sid = h.session_token_stats()
    if sid:
        lines.append(f"  Tokens:     {total_tokens:,} across {msg_count} turns this session")
    else:
        lines.append("  Tokens:     (session not persisted yet)")
    cs = ctx.control_server
    if cs is not None:
        pending = cs.has_pending_approval(h.session_name)
        steps = len(cs.turn_recap(h.session_name))
        lines.append(
            f"  Tools:      {steps} this turn"
            + (" | ⏸️ AWAITING /approve or /deny" if pending else "")
        )
    return "\n".join(lines)


def _cmd_pause(ctx: CommandContext, args: str) -> str | None:
    reason = args.strip().strip('"')
    return ctx.hermes.pause(reason)


def _cmd_resume(ctx: CommandContext, args: str) -> str | None:
    return ctx.hermes.resume()


def _cmd_retry(ctx: CommandContext, args: str) -> str | None:
    last = ctx.hermes.get_last_prompt()
    if not last:
        return "Nothing to retry — no prior prompt this session."
    # Re-run the last prompt. The gateway session already holds full
    # context, so we just resend the user's own message to the model.
    return ctx.hermes.chat(last)


def _cmd_usage(ctx: CommandContext, args: str) -> str | None:
    total_tokens, msg_count, sid = ctx.hermes.session_token_stats()
    if sid is None:
        return "No session persisted yet — no usage to report."
    return f"📈 Session usage ({sid[:12]}…): {total_tokens:,} tokens across {msg_count} turns."


def _cmd_version(ctx: CommandContext, args: str) -> str | None:
    return f"🏷️ {ctx.hermes.version()}"


def _cmd_help(ctx: CommandContext, args: str) -> str | None:
    lines = [
        "Hermes endpoint commands:",
        "",
        "/model — list models, switch, or reset the model",
        "/new — start a fresh conversation (clears context)",
        "/stop — kill the running Hermes process",
        "/approve — allow the pending risky tool to run",
        "/deny — deny the pending tool and abort this turn",
        "/steer <text> — queue an instruction for the next turn",
        "/tools — recap of tool calls this turn",
        "/verbose on|off — include tool args/results in recaps",
        "/status — bridge status (model, session, process, tokens)",
        "/pause [reason] — halt new Hermes work (emergency stop)",
        "/resume — lift the emergency stop",
        "/retry — resend the last prompt",
        "/usage — token usage for this session",
        "/version — Hermes Agent version",
        "/help — show this message",
    ]
    return "\n".join(lines)


def _cmd_new(ctx: CommandContext, args: str) -> str | None:
    if args.strip().lower() in ("help", "-h", "?"):
        return "Usage: /new\nStarts a brand-new conversation, discarding prior context."
    ctx.hermes.reset_session()
    return "🔄 Started a new session. Context cleared — next message begins fresh."


# ── tool-approval / steering commands (need the control server) ──────


def _ctrl(ctx: CommandContext):
    cs = ctx.control_server
    if cs is None:
        return None, "Control endpoint not running."
    return cs, None


def _cmd_approve(ctx: CommandContext, args: str) -> str | None:
    cs, err = _ctrl(ctx)
    if err:
        return err
    session = ctx.hermes.session_name
    if not session:
        return "No active mesh session."
    if cs.answer_approval(session, approve=True):
        return "✅ Approved — tool may proceed."
    return "No pending approval to approve."


def _cmd_deny(ctx: CommandContext, args: str) -> str | None:
    cs, err = _ctrl(ctx)
    if err:
        return err
    session = ctx.hermes.session_name
    if not session:
        return "No active mesh session."
    if cs.answer_approval(session, approve=False):
        return "⛔ Denied — aborting this turn."
    return "No pending approval to deny."


def _cmd_steer(ctx: CommandContext, args: str) -> str | None:
    cs, err = _ctrl(ctx)
    if err:
        return err
    text = args.strip()
    if not text:
        return "Usage: /steer <instruction> — queued for the next turn."
    session = ctx.hermes.session_name
    if not session:
        return "No active mesh session."
    cs.queue_steer(session, text)
    return f"📌 Steering queued for next turn:\n{text}"


def _cmd_verbose(ctx: CommandContext, args: str) -> str | None:
    verbose = args.strip().lower() in ("on", "1", "yes", "true")
    if verbose == "":
        cur = bool(ctx.state.get("verbose", False))
        return f"Verbose tool detail: {'on' if cur else 'off'} (use /verbose on|off)"
    ctx.state["verbose"] = verbose
    return f"Verbose tool detail: {'on' if verbose else 'off'}"


def _cmd_tools(ctx: CommandContext, args: str) -> str | None:
    cs, err = _ctrl(ctx)
    if err:
        return "No tool events recorded yet."
    session = ctx.hermes.session_name
    steps = cs.turn_recap(session) if session else []
    if not steps:
        return "No tool calls this turn yet."
    lines = [f"🔧 Tools this turn ({len(steps)}):"]
    for s in steps[-10:]:
        lines.append(f"  {s.summary()}" + (" ❌" if s.is_error else ""))
    if len(steps) > 10:
        lines.append(f"  … and {len(steps) - 10} earlier")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Registry — add new commands here
# ──────────────────────────────────────────────────────────────────────

COMMANDS: dict[str, CommandFn] = {
    "/model": lambda ctx, args: ctx.model_handler.run(f"/model {args}".strip()),
    "/new": _cmd_new,
    "/stop": _cmd_stop,
    "/status": _cmd_status,
    "/pause": _cmd_pause,
    "/resume": _cmd_resume,
    "/retry": _cmd_retry,
    "/usage": _cmd_usage,
    "/version": _cmd_version,
    "/help": _cmd_help,
    "/commands": _cmd_help,
    # Tool-approval / steering (need the control server at runtime).
    "/approve": _cmd_approve,
    "/deny": _cmd_deny,
    "/steer": _cmd_steer,
    "/verbose": _cmd_verbose,
    "/tools": _cmd_tools,
}


class CommandDispatcher:
    """Routes incoming text to a command handler, if any matches.

    ``handle`` returns the handler's reply string, or ``None`` if the text
    is not a recognized command (caller should then forward to Hermes).
    """

    def __init__(self, ctx: CommandContext):
        self.ctx = ctx

    def handle(self, text: str) -> str | None:
        text = (text or "").strip()
        if not text or not text.startswith("/"):
            return None

        # First token is the command word (e.g. "/model").
        parts = text.split(None, 1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Normalize aliases (e.g. "/model" vs "model").
        if command in ("model", "m"):
            command = "/model"
        elif command in ("new",):
            command = "/new"
        elif command in ("help", "commands"):
            command = "/help"
        elif command in ("stop",):
            command = "/stop"
        elif command in ("approve", "a"):
            command = "/approve"
        elif command in ("deny", "d"):
            command = "/deny"
        elif command in ("steer",):
            command = "/steer"
        elif command in ("verbose",):
            command = "/verbose"
        elif command in ("tools", "t"):
            command = "/tools"
        elif command in ("status", "s"):
            command = "/status"
        elif command in ("pause", "p"):
            command = "/pause"
        elif command in ("resume",):
            command = "/resume"
        elif command in ("retry", "r"):
            command = "/retry"
        elif command in ("usage", "u"):
            command = "/usage"
        elif command in ("version", "v"):
            command = "/version"

        handler = COMMANDS.get(command)
        if handler is None:
            # Not a known command — could be a model name typed bare; treat
            # as non-command so it reaches the model.
            logger.debug("No command handler for %r", command)
            return None

        try:
            result = handler(self.ctx, args)
        except Exception:
            logger.exception("Command handler failed for %r", command)
            return f"❌ Command {command} errored. Check the logs."
        return result


# ──────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────


def build_dispatcher(
    hermes: HermesClient,
    model_handler: ModelCommandHandler,
    control_server: ControlServer | None = None,
) -> CommandDispatcher:
    ctx = CommandContext(
        hermes=hermes,
        model_handler=model_handler,
        control_server=control_server,
    )
    return CommandDispatcher(ctx)
