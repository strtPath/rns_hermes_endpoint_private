#!/usr/bin/env python3
"""
CLI entry point for Hermes for Reticulum.

Usage:
    hermes-reticulum              # Start the bridge (default)
    hermes-reticulum --announce   # Start and announce
    hermes-reticulum --status     # Show bridge status
    hermes-reticulum --address    # Print the LXMF address only
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from hermes_reticulum import __version__
from hermes_reticulum.core.acl import AccessControl
from hermes_reticulum.core.bridge import LXMFBridge
from hermes_reticulum.core.commands import build_dispatcher
from hermes_reticulum.core.control_server import ControlServer
from hermes_reticulum.core.hermes_client import HermesClient
from hermes_reticulum.core.model_command import ModelCommandHandler


def _load_dotenv():
    """Load .env file from project root if python-dotenv is available."""
    try:
        from dotenv import load_dotenv

        # Look for .env in CWD and project root
        for candidate in [Path.cwd() / ".env", Path(__file__).resolve().parent.parent.parent / ".env"]:
            if candidate.exists():
                load_dotenv(candidate)
                return
    except ImportError:
        pass  # python-dotenv not installed — rely on OS env only


def setup_logging(verbose: bool = False):
    """Configure logging for the bridge."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S")


def _deny_veto(hermes) -> None:
    """Abort the in-flight hermes child when the mesh operator denies a tool."""
    hermes.stop()


def cmd_run(args):
    """Start the bridge and run until interrupted."""
    setup_logging(args.verbose)
    logger = logging.getLogger("hermes_reticulum.cli")

    # Build configuration from env / args
    display_name = args.display_name or os.getenv("RETICULUM_DISPLAY_NAME", "Hermes for Reticulum")
    storage = args.storage or os.getenv("RETICULUM_STORAGE", None)
    stamp_cost = args.stamp_cost or int(os.getenv("RETICULUM_STAMP_COST", "8"))
    rns_config = args.rns_config or os.getenv("RETICULUM_CONFIG", None)
    hermes_bin = args.hermes_bin or os.getenv("HERMES_BIN", "hermes")
    timeout = args.timeout or int(os.getenv("HERMES_TIMEOUT", "300"))

    # Initialize components
    acl = AccessControl()
    hermes = HermesClient(hermes_bin=hermes_bin, timeout=timeout)

    # /model command handler (mirrors the Telegram gateway's /model).
    model_cmd = ModelCommandHandler(hermes)

    # Local control endpoint: receives agent:step tool events from the
    # gateway hook (HTTP POST) and serves /approve /deny /stop /steer.
    ctrl = ControlServer(storage_path=storage)
    ctrl.on_deny = lambda session: _deny_veto(hermes)  # veto: kill the child
    # Push live tool events to the mesh as they happen. The hook POSTs each
    # step here; we reply with a short one-liner (bandwidth-aware).
    mesh_push: dict[str, dict] = {}  # mesh title -> {"hash": source_hash, "ident": dest}

    def _on_tool_step(session: str, step) -> None:
        label = getattr(step, "name", None) or str(step)
        push = mesh_push.get(session)
        if push is None:
            logger.debug("no mesh peer for %r — dropping tool event", session)
            return
        ok = bridge.send_reply(push["hash"], f"🔧 {label}", push["ident"])
        if ok:
            logger.info("Tool event %s pushed to mesh peer %s", label, push["hash"][:16])
        else:
            logger.warning("Failed to push tool event %s to mesh", label)

    ctrl.on_step = _on_tool_step

    # Pre-execution gate: when the mesh-tool-gate plugin asks the mesh operator
    # to approve/deny a risky tool *before* it runs, push the pending command
    # to the mesh peer. (The old /step gate never wired on_gate_open; this is
    # the pre-exec path.) on_deny stays unwired for /gate/notify — a pre-exec
    # block is handled by the plugin returning a block directive, not by
    # killing the child.
    def _on_gate_open(session: str, tool: str, command: str, description: str) -> None:
        push = mesh_push.get(session)
        if push is None:
            logger.debug("no mesh peer for %r — dropping gate-open", session)
            return
        body = (
            f"⚠️ PRE-EXEC APPROVAL\n"
            f"Tool: {tool}\n"
            f"Command: {command}\n"
            f"({description})\n"
            f"Reply /approve to run it, /deny to block."
        )
        ok = bridge.send_reply(push["hash"], body, push["ident"])
        if ok:
            logger.info(
                "Gate-open pushed for %s → mesh peer %s",
                tool, push["hash"][:16],
            )
        else:
            logger.warning("Failed to push gate-open for %s to mesh", tool)

    ctrl.on_gate_open = _on_gate_open

    # Step-through mode: the hook POSTs the full tool call + full output
    # to /step/full.  We chunk it into ≤1500-char LXMF posts and send
    # them one by one (user-approved bandwidth cost).
    def _on_full_step(session: str, text: str) -> None:
        push = mesh_push.get(session)
        if push is None:
            logger.debug("no mesh peer for %r — dropping full step", session)
            return
        ok = bridge.push_reply(push["hash"], text, push["ident"])
        if ok:
            logger.info(
                "Full step pushed to mesh peer %s (%d chars)",
                push["hash"][:16], len(text),
            )
        else:
            logger.warning("Failed to push full step to mesh for %s", session)

    ctrl.on_full_step = _on_full_step
    # CLI-side step watcher push callback: while a mesh turn runs, the
    # watcher (in hermes_client.chat) pushes each tool call + output as its
    # own chunked 💻 message to the peer that sent the message. The bridge's
    # child is a CLI process, so the gateway's agent:step hook never fires
    # for it — this is the path that actually delivers the per-tool messages.
    def _step_push(text: str) -> None:
        push = mesh_push.get(hermes.session_name)
        if push is None:
            logger.debug("no mesh peer for step push — dropping")
            return
        bridge.push_reply(push["hash"], text, push["ident"])

    hermes.set_push_callback(_step_push)
    if not ctrl.start():
        logger.warning(
            "Control endpoint not started — /approve /deny /steer and live "
            "tool streaming disabled (recap fallback still works)."
        )

    # Generic slash-command dispatcher — adds /model, /new, /help, /commands.
    # Add more in core/commands.py (COMMANDS dict); no cli.py changes needed.
    dispatcher = build_dispatcher(hermes, model_cmd, ctrl)

    bridge = LXMFBridge(
        display_name=display_name,
        storage_path=storage,
        stamp_cost=stamp_cost,
        rns_config_path=rns_config,
    )

    # Wire up: LXMF message → ACL check → command dispatch → profile → Hermes → reply
    def handle_message(source_hash: str, content: str, profile=None) -> str | None:
        if not acl.is_allowed(source_hash):
            logger.info("Message from %s rejected by ACL", source_hash[:16])
            return "⛔ Access not authorized."

        # Track this peer so live tool events can be pushed to it.
        try:
            import RNS  # noqa: F401

            ident = RNS.Identity.recall(bytes.fromhex(source_hash))
            if ident is not None:
                mesh_push[hermes.session_name] = {
                    "hash": source_hash,
                    "ident": RNS.Destination(
                        ident, RNS.Destination.OUT, RNS.Destination.SINGLE,
                        "lxmf", "delivery",
                    ),
                }
        except Exception:
            pass

        # Slash commands — handled locally, never sent to the LLM.
        command_reply = dispatcher.handle(content)
        if command_reply is not None:
            logger.info("Command from %s: %s", source_hash[:16], content)
            return command_reply

        # Build prompt with adaptive instruction from profile
        if profile and profile.instruction:
            prompt = f"{profile.instruction}\n\nUser: {content}"
        else:
            prompt = content

        return hermes.chat(prompt)

    bridge.set_message_handler(handle_message)

    # Start
    logger.info("Display name: %s", display_name)
    logger.info("ACL mode: %s", acl.mode)
    logger.info("Hermes binary: %s", hermes_bin)
    logger.info("Timeout: %ds", timeout)

    bridge.run_forever()


def _delivery_address(storage: str) -> str:
    """
    Compute the LXMF *delivery-destination* hash for the on-disk identity.

    This is the address the running bridge actually announces and replies from —
    i.e. ``LXMRouter.register_delivery_identity`` ->
    ``RNS.Destination(identity, IN, SINGLE, "lxmf", "delivery").hash``.

    Do NOT print ``identity.hash`` here: that is the bare *identity* hash, which
    is a different 32-byte value and is NOT the address a client should message.
    Confusing the two is what made the bridge look like it was "changing
    identities" across restarts (see docs/mesh-bridge-findings-2026-08-27-
    identity-changes-on-restart.md).
    """
    import RNS

    RNS.Reticulum()  # init shared instance so Destination can register
    identity = RNS.Identity.from_file(os.path.join(storage, "hermes_identity"))
    destination = RNS.Destination(
        identity, RNS.Destination.IN, RNS.Destination.SINGLE, "lxmf", "delivery"
    )
    return RNS.prettyhexrep(destination.hash)


def cmd_address(args):
    """Print the LXMF delivery address of the existing identity."""
    setup_logging(False)

    storage = args.storage or os.getenv("RETICULUM_STORAGE", os.path.expanduser("~/.lxmf/storage"))
    identity_path = os.path.join(storage, "hermes_identity")

    if not os.path.exists(identity_path):
        print(f"No identity found at {identity_path}")
        print("Run 'hermes-reticulum' first to generate one.")
        sys.exit(1)

    print(f"LXMF Address: {_delivery_address(storage)}")


def cmd_status(args):
    """Show bridge status."""
    setup_logging(False)

    storage = args.storage or os.getenv("RETICULUM_STORAGE", os.path.expanduser("~/.lxmf/storage"))
    identity_path = os.path.join(storage, "hermes_identity")
    acl = AccessControl()

    print("═══ Hermes for Reticulum ═══")
    print(f"  Version:   {__version__}")
    print(f"  Storage:   {storage}")
    print(f"  Identity:  {'found' if os.path.exists(identity_path) else 'not created yet'}")
    print(f"  ACL mode:  {acl.mode}")
    print()

    if os.path.exists(identity_path):
        import RNS

        print(f"  Address:   {_delivery_address(storage)}")

        # Show RNS interfaces
        print()
        print("  RNS Interfaces:")
        try:
            rns = RNS.Reticulum()
            for iface in rns.interfaces:
                print(f"    - {iface}")
        except Exception:
            print("    (unable to list interfaces)")
    else:
        print("  Run 'hermes-reticulum' to initialize.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-reticulum",
        description="Hermes for Reticulum — AI agent on the mesh network",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--storage", "-s", help="Storage path for identity/messages (default: env RETICULUM_STORAGE)"
    )

    sub = parser.add_subparsers(dest="command", help="Command to run")

    run_p = sub.add_parser(
        "run",
        help="Start the bridge",
        parents=[common],
        description="Start the bridge (default when no subcommand is given).",
    )
    run_p.add_argument("--display-name", "-n", help="Display name on the mesh")
    run_p.add_argument("--stamp-cost", type=int, help="LXMF stamp cost")
    run_p.add_argument("--rns-config", help="Path to Reticulum config dir")
    run_p.add_argument("--hermes-bin", help="Path to hermes CLI binary")
    run_p.add_argument("--timeout", type=int, help="Hermes timeout in seconds")
    run_p.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    run_p.set_defaults(func=cmd_run)

    sub.add_parser("address", help="Show LXMF address", parents=[common]).set_defaults(
        func=cmd_address
    )
    sub.add_parser("status", help="Show bridge status", parents=[common]).set_defaults(
        func=cmd_status
    )
    return parser


def main():
    _load_dotenv()  # Load .env before any command parsing
    parser = _build_parser()
    argv = sys.argv[1:]

    # No subcommand → default to "run".
    #
    # "run"'s flags are a superset of the parser's own options (--storage /
    # --verbose are shared via the `common` parent), so the single re-parse is
    # valid: every argv token a bare invocation can carry is also recognized
    # by the "run" parser. (Do NOT pre-parse with the main parser first — the
    # old default path re-ran the main parser over argv, which rejected
    # run-only flags like --stamp-cost with "unrecognized arguments".)
    if not argv or argv[0].startswith("-") or argv[0] not in ("run", "address", "status"):
        argv = ["run"] + argv

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
