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

logger = logging.getLogger("hermes_reticulum.commands")


@dataclass
class CommandContext:
    """Per-bridge state handed to every command handler."""

    hermes: HermesClient
    model_handler: ModelCommandHandler
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


def _cmd_help(ctx: CommandContext, args: str) -> str | None:
    lines = [
        "Hermes endpoint commands:",
        "",
        "/model — list models, switch, or reset the model",
        "/new — start a fresh conversation (clears context)",
        "/stop — kill the running Hermes process",
        "/help — show this message",
    ]
    return "\n".join(lines)


def _cmd_new(ctx: CommandContext, args: str) -> str | None:
    if args.strip().lower() in ("help", "-h", "?"):
        return "Usage: /new\nStarts a brand-new conversation, discarding prior context."
    ctx.hermes.reset_session()
    return "🔄 Started a new session. Context cleared — next message begins fresh."


# ──────────────────────────────────────────────────────────────────────
# Registry — add new commands here
# ──────────────────────────────────────────────────────────────────────

COMMANDS: dict[str, CommandFn] = {
    "/model": lambda ctx, args: ctx.model_handler.run(f"/model {args}".strip()),
    "/new": _cmd_new,
    "/stop": _cmd_stop,
    "/help": _cmd_help,
    "/commands": _cmd_help,
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


def build_dispatcher(hermes: HermesClient, model_handler: ModelCommandHandler) -> CommandDispatcher:
    ctx = CommandContext(hermes=hermes, model_handler=model_handler)
    return CommandDispatcher(ctx)
