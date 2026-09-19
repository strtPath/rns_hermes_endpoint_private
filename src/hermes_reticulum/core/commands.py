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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from hermes_reticulum.core.hermes_client import HermesClient
from hermes_reticulum.core.model_command import ModelCommandHandler
from hermes_reticulum.core.control_server import ControlServer
from hermes_reticulum.core.bridge import MIN_ANNOUNCE_INTERVAL_MIN

logger = logging.getLogger("hermes_reticulum.commands")


@dataclass
class CommandContext:
    """Per-bridge state handed to every command handler."""

    hermes: HermesClient
    model_handler: ModelCommandHandler
    # ControlServer instance (None until the CLI wires it).
    control_server: ControlServer | None = None
    # The running LXMFBridge (None in tests). Loosely typed to avoid an
    # import cycle; handlers use getattr. Used for /status liveness state.
    bridge: object | None = None
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


def _append_liveness(lines: list, ctx: CommandContext) -> None:
    """Append the bridge-level liveness (Tier 4.5) line to /status output."""
    bridge = getattr(ctx, "bridge", None)
    lv = getattr(bridge, "liveness", None)
    if lv is None:
        lines.append("  Watchdog:   (not started — foreground/dev)")
        return
    snap = lv.snapshot()
    if snap["rns_healthy"]:
        age = snap.get("last_tick_age_s")
        age_str = f"{int(age)}s ago" if age is not None else "never"
        lines.append(f"  Watchdog:   ✓ RNS alive (heartbeat {age_str})")
    else:
        lines.append("  Watchdog:   ✗ RNS not responsive (systemd will restart)")


def _cmd_status(ctx: CommandContext, args: str) -> str | None:
    h = ctx.hermes
    session_line = (
        f"  Session:    {h.session_name}"
        + (f" ({h._resume_id[:12]}…)" if h._resume_id else " (unpinned)")
    )
    lines = [
        "📊 Bridge status",
        f"  Model:      {h.get_model()}",
        session_line,
    ]
    cs = ctx.control_server
    # Bridge service health (uptime from the control endpoint)
    if cs is not None:
        uptime_s = time.monotonic() - cs._started
        if uptime_s < 60:
            uptime_str = f"up {int(uptime_s)}s"
        elif uptime_s < 3600:
            m, s = divmod(int(uptime_s), 60)
            uptime_str = f"up {m}m{s:02d}s"
        else:
            h_uptime, rem = divmod(int(uptime_s), 3600)
            m, s = divmod(rem, 60)
            uptime_str = f"up {h_uptime}h{m:02d}m"
        lines.append(f"  Bridge:     {uptime_str}")
    else:
        lines.append("  Bridge:     (control endpoint not wired)")
    # Preflight health
    from hermes_reticulum.core.preflight import run_preflight
    hermes_bin = getattr(h, "hermes_bin", None)
    pf = run_preflight(hermes_bin=hermes_bin)
    if pf.ok:
        lines.append("  Preflight:  ✓ ok")
    else:
        err_count = len(pf.errors)
        warn_count = len(pf.warnings)
        lines.append(f"  Preflight:  ✗ {err_count} error(s), {warn_count} warning(s)")
        for e in pf.errors:
            lines.append(f"    ✗ {e}")
    # Bridge-level liveness (Tier 4.5): RNS-wedge watchdog state.
    _append_liveness(lines, ctx)
    lines.append(f"  Turn:       {'in-flight' if h.is_running() else 'idle'}")
    lines.append(f"  Hard cap:   {h.timeout}s")
    lines.append(
        f"  Liveness:   {h.liveness_timeout}s"
        + (" (off)" if not h.liveness_timeout else "")
    )
    total_tokens, msg_count, sid = h.session_token_stats()
    if sid:
        lines.append(f"  Tokens:     {total_tokens:,} across {msg_count} turns this session")
    else:
        lines.append("  Tokens:      (not tracked by bridge)")
    if cs is not None:
        pending = cs.has_pending_approval(h.session_name)
        total = cs.session_tool_total(h.session_name)
        turn = len(cs.turn_recap(h.session_name))
        suffix = " | ⏸️ AWAITING /approve or /deny" if pending else ""
        lines.append(f"  Tools:      {total} this session ({turn} this turn){suffix}")
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
        "/steps on|off — full tool call + output before each next action",
        "/hold — pause the final reply (checkpoint gate)",
        "/go — release a /hold",
        "/announce [minutes] — re-announce the bridge on the mesh now; "
        "with <minutes>, also set the periodic re-announce cadence for this run",
        "",
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
    reason = args.strip().strip('"')
    if cs.answer_approval(session, approve=False, reason=reason):
        if reason:
            return f"⛔ Denied — {reason}."
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
    if not args.strip():
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
    lines = [f"📋 Tools this turn ({len(steps)}):"]
    for s in steps[-10:]:
        # summary() already carries the error glyph — don't double it.
        lines.append(f"  {s.summary()}")
    if len(steps) > 10:
        lines.append(f"  … and {len(steps) - 10} earlier")
    return "\n".join(lines)


# ── step-through mode ────────────────────────────────────────────────


def _step_mode_file_path() -> str:
    """Path of the state file the hook (gateway process) polls for mode."""
    from hermes_reticulum.core.bridge import MODE_STATE_PATH
    return MODE_STATE_PATH


def _cmd_steps(ctx: CommandContext, args: str) -> str | None:
    """Toggle step-through mode: full tool call + output before next action."""
    arg = args.strip().lower()
    if arg in ("on", "1", "yes", "true"):
        # hermes_client flag (prompt prefix) + the state file the hook polls.
        ctx.hermes.set_step_mode(True)
        try:
            Path(_step_mode_file_path()).write_text("1")
        except OSError:
            pass
        return (
            "👁️ Step-through ON: each tool call and its full output will be "
            "posted to you as its own message before the model proceeds "
            "(💻 <tool> <args> <output>). Use /steps off to stop, /hold to "
            "pause the final reply until /go."
        )
    if arg in ("off", "0", "no", "false"):
        ctx.hermes.set_step_mode(False)
        try:
            Path(_step_mode_file_path()).write_text("0")
        except OSError:
            pass
        return "👁️ Step-through OFF: back to the default terse recap."
    # No arg: report current state (from the file, the source of truth)
    try:
        cur = Path(_step_mode_file_path()).read_text().strip() == "1"
    except OSError:
        cur = False
    state = "ON" if cur else "off"
    return (
        f"Step-through: {state}\n"
        "Usage: /steps on|off\n"
        "  on  — full tool call + output posted in chunks before each "
        "next action (uses more mesh bandwidth)\n"
        "  off — default terse recap\n"
        "Companions: /hold (pause reply), /go (release hold)"
    )


def _cmd_hold(ctx: CommandContext, args: str) -> str | None:
    """Pause the final reply until /go is sent (checkpoint gate)."""
    if not ctx.hermes.is_running():
        return "Nothing to hold — no turn running."
    ctx.hermes.set_hold_gate(True)
    return (
        "⏸ Held — the final reply will be gated until you send /go.\n"
        "(Auto-releases in ~30 min if you forget.)"
    )


def _cmd_go(ctx: CommandContext, args: str) -> str | None:
    """Release a /hold gate so the final reply can leave the bridge."""
    if ctx.hermes.is_step_mode() and not ctx.hermes._hold_gate:
        # /go with no active hold: treat as a no-op confirmation
        return "Not held — nothing to release."
    ctx.hermes.set_hold_gate(False)
    return "🚀 Released — sending the reply."


# ── announce (re-announce the bridge on the mesh) ──────────────────────


def _fmt_interval(minutes: float) -> str:
    """Human-readable cadence: whole minutes, otherwise one decimal."""
    if minutes == int(minutes):
        return f"{int(minutes)} min"
    return f"{minutes:.1f} min"


def _cmd_announce(ctx: CommandContext, args: str) -> str | None:
    """Re-announce the bridge's destination on the Reticulum mesh.

    Bare ``/announce`` re-announces immediately and keeps the current live
    cadence. ``/announce <minutes>`` also sets the periodic re-announce
    cadence for the *current run* (persisted cadence still comes from
    RETICULUM_ANNOUNCE_INTERVAL in .env, applied at bridge start). 0 disables
    periodic re-announce; values below MIN_ANNOUNCE_INTERVAL_MIN are rejected.
    """
    bridge = getattr(ctx, "bridge", None)
    dest = getattr(bridge, "destination", None)
    if dest is None:
        return "Bridge not started — nothing to announce yet."

    arg = args.strip().lower()
    if arg and arg not in ("help", "-h", "?"):
        try:
            interval = float(arg)
        except ValueError:
            return (
                "Usage: /announce [minutes]\n"
                "  (no arg) — re-announce now (keeps current cadence)\n"
                f"  <minutes> — re-announce now AND set the periodic "
                f"re-announce cadence for this run (min {MIN_ANNOUNCE_INTERVAL_MIN:g}; "
                "0 disables)"
            )
        if interval < 0:
            return "Interval must be ≥ 0."
        if 0 < interval < MIN_ANNOUNCE_INTERVAL_MIN:
            return (
                f"Interval too small: {interval:g} min. "
                f"Use at least {MIN_ANNOUNCE_INTERVAL_MIN:g} min (or 0 to disable) — "
                "smaller values would spam the mesh."
            )
        if interval > 0:
            # Restart the periodic timer with the new cadence (announce()
            # re-announces immediately + restarts the scheduler).
            try:
                bridge.announce(interval_min=interval)
            except ValueError as e:
                return str(e)
            return (
                f"📡 Re-announced {bridge.address} on the mesh.\n"
                f"Periodic re-announce now every {_fmt_interval(interval)} for "
                "this run."
            )
        # interval == 0 → disable periodic re-announce, still re-announce now.
        try:
            bridge.announce(interval_min=0.0)
        except ValueError as e:
            return str(e)
        return (
            f"📡 Re-announced {bridge.address} on the mesh.\n"
            "Periodic re-announce DISABLED for this run "
            "(set RETICULUM_ANNOUNCE_INTERVAL>0 in .env and restart to enable)."
        )

    # Bare /announce: re-announce now, keep the current live cadence.
    try:
        bridge.announce()
    except ValueError as e:
        return str(e)
    cur = getattr(bridge, "announce_interval_min", 0.0)
    env_default = getattr(bridge, "env_announce_interval_min", cur)
    cadence = (
        f"every {_fmt_interval(cur)}" if cur > 0 else "disabled (interval=0)"
    )
    return (
        f"📡 Re-announced {bridge.address} on the mesh.\n"
        f"Periodic re-announce: {cadence} "
        f"(env default {_fmt_interval(env_default)} via RETICULUM_ANNOUNCE_INTERVAL)."
    )


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
    # Step-through mode
    "/steps": _cmd_steps,
    "/hold": _cmd_hold,
    "/go": _cmd_go,
    # Mesh presence
    "/announce": _cmd_announce,
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
        word = command.lstrip("/")
        if word in ("model", "m"):
            command = "/model"
        elif word in ("new",):
            command = "/new"
        elif word in ("help", "commands"):
            command = "/help"
        elif word in ("stop",):
            command = "/stop"
        elif word in ("approve", "a"):
            command = "/approve"
        elif word in ("deny", "d"):
            command = "/deny"
        elif word in ("steer",):
            command = "/steer"
        elif word in ("verbose",):
            command = "/verbose"
        elif word in ("tools", "t"):
            command = "/tools"
        elif word in ("status", "s"):
            command = "/status"
        elif word in ("pause", "p"):
            command = "/pause"
        elif word in ("resume",):
            command = "/resume"
        elif word in ("retry", "r"):
            command = "/retry"
        elif word in ("usage", "u"):
            command = "/usage"
        elif word in ("version", "v"):
            command = "/version"
        elif word in ("announce",):
            command = "/announce"

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
    bridge=None,
) -> CommandDispatcher:
    ctx = CommandContext(
        hermes=hermes,
        model_handler=model_handler,
        control_server=control_server,
        bridge=bridge,
    )
    return CommandDispatcher(ctx)
