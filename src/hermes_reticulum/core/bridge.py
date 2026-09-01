"""
LXMF Message Bridge — the core engine that connects Reticulum/LXMF to Hermes Agent.

Manages the LXM Router, receives messages from mesh LXMF clients (RNodes, Sideband, etc.),
dispatches them to Hermes, and sends replies back over the mesh.

Thread safety: The LXMF delivery callback runs inside the RNS event loop
thread. To avoid blocking the mesh stack, we dispatch message handling to
a separate thread pool so that long-running operations (like calling the
Hermes CLI) don't stall incoming message processing.
"""

import logging
import os
import signal
import threading
import time
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import LXMF
import RNS

from hermes_reticulum.core.adapter import prepare_reply, split_message
from hermes_reticulum.core.profiler import ChannelMetrics, ChannelProfiler

logger = logging.getLogger("hermes_reticulum.bridge")

# Max concurrent message handlers — prevents hermes subprocess flood
_MAX_HANDLERS = 4

# Chunked tool I/O delivery ("steps mode"): cap per LXMF post.
# 1500 chars ≈ 4 standard ~368-byte content blocks, keeps LoRa sane,
# TCP delivers instantly. User-approved bandwidth cost.
STEP_CHUNK_CHARS = 1500

# Step-through checkpoint gate.
#
# Lives on the BRIDGE side (hermes-reticulum process), not in the hook:
# the hook runs inside the gateway process and can't see the control
# server, but /hold and /go are dispatched here.  contextvars make it
# safe across the thread pool (each turn = one context).
_step_mode_var: ContextVar[str] = ContextVar("reticulum_step_mode", default="off")
_hold_release_var: ContextVar[bool] = ContextVar("reticulum_hold_release", default=False)
_hold_active_var: ContextVar[bool] = ContextVar("reticulum_hold_active", default=False)

# Hold timeout (seconds): if the user never says /go, release anyway so
# the reply is never lost. 0 = hold indefinitely.
HOLD_TIMEOUT_S = float(os.environ.get("HERMES_STEP_HOLD_TIMEOUT", "1800"))

# Hold state file: bridge writes, hook (gateway process) polls.
HOLD_STATE_PATH = os.environ.get(
    "HERMES_STEP_HOLD_FILE",
    os.path.expanduser("~/.hermes/.reticulum-hold-state"),
)

# Mode state file: bridge writes, hook (gateway process) polls.
MODE_STATE_PATH = os.environ.get(
    "HERMES_STEP_MODE_FILE",
    os.path.expanduser("~/.hermes/.reticulum-step-mode"),
)


class StepThroughManager:
    """
    Step-through mode state: "print the full tool call and full output
    before the model moves on" over mesh.

    What this mode does (and doesn't):

    - The hook (agent:step, fires AFTER each tool batch) pushes the FULL
      tool call arguments and full output to the user, chunked across
      multiple LXMF posts, instead of a one-line truncated recap.
    - It also injects a "step-through active" prefix into the prompt so
      the model reports each tool it's about to run before running it —
      the closest thing to "show before next action" that the mesh
      transport allows (agent:step has no veto; the tool has already run).
    - Checkpoint gate: while a turn is running, `/hold` sets a flag; the
      bridge then waits for `/go` (or the timeout) before releasing the
      final reply to the mesh.  This is an opt-in pause on the reply,
      not a pre-tool-execution gate.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.enabled = False
        self.hold_requested = False

    # ── mode ─────────────────────────────────────────────────────────

    def set_mode(self, enabled: bool):
        with self._lock:
            self.enabled = bool(enabled)
        # The hook (gateway process) reads this file to know whether to
        # stream full tool I/O.  Bridge writes, hook polls.
        try:
            Path(MODE_STATE_PATH).write_text("1" if enabled else "0")
        except OSError as e:
            logger.debug("Could not write mode state file: %s", e)

    def is_enabled(self) -> bool:
        with self._lock:
            return self.enabled

    # ── hold gate (checkpoint) ───────────────────────────────────────

    def request_hold(self) -> None:
        """User pressed /hold: gate the next final reply until /go."""
        self.hold_requested = True
        _hold_active_var.set(True)
        self._write_hold_state(True)

    def release_hold(self) -> None:
        """User pressed /go: release the gated reply."""
        self.hold_requested = False
        _hold_release_var.set(True)
        self._write_hold_state(False)

    def _write_hold_state(self, held: bool) -> None:
        try:
            Path(HOLD_STATE_PATH).write_text("1" if held else "0")
        except OSError as e:
            logger.debug("Could not write hold state file: %s", e)

    def gate_and_wait(self, source_hash: str, push) -> None:
        """
        Block until hold is released (or timeout) before the final reply
        is sent.  Only blocks if /hold was requested for THIS turn.

        Call from the bridge's thread-pool worker (has source identity +
        push capability), never from the hook/gateway process.
        """
        if not self.hold_requested:
            return

        push("⏸ held — send /go to release (or it releases on its own)")

        deadline = None
        if HOLD_TIMEOUT_S > 0:
            deadline = time.time() + HOLD_TIMEOUT_S

        while self.hold_requested:
            if deadline is not None and time.time() >= deadline:
                break
            time.sleep(1)

        self.hold_requested = False
        _hold_release_var.set(True)
        self._write_hold_state(False)


# Module-level default manager (the bridge instance owns its own copy).
_default_step_through = StepThroughManager()


def step_through_manager() -> StepThroughManager:
    """The manager owned by the running bridge, or the module default."""
    return _default_step_through


class LXMFBridge:
    """
    Bridges LXMF messages to/from Hermes Agent.

    This class owns the RNS instance, the LXM Router, and the identity.
    It receives incoming LXMF messages, invokes a configurable handler
    (which can call Hermes via CLI, API, or direct import), and sends
    replies back over the same LXMF channel.
    """

    def __init__(
        self,
        display_name: str = "Hermes for Reticulum",
        storage_path: str | Path | None = None,
        stamp_cost: int = 8,
        enforce_stamps: bool = False,
        rns_config_path: str | Path | None = None,
    ):
        """
        Initialize the LXMF bridge.

        Args:
            display_name: Human-readable name announced on the mesh.
            storage_path: Where to persist LXMF messages and identity.
            stamp_cost: LXMF stamp cost (bandwidth throttle).
            enforce_stamps: Whether to require valid stamps from senders.
            rns_config_path: Path to Reticulum config directory (None = default).
        """
        self.display_name = display_name
        self.storage_path = Path(storage_path or os.path.expanduser("~/.lxmf/storage"))
        self.stamp_cost = stamp_cost
        self.enforce_stamps = enforce_stamps
        self.rns_config_path = str(rns_config_path) if rns_config_path else None

        # Will be set during start()
        self.reticulum: RNS.Reticulum | None = None
        self.router: LXMF.LXMRouter | None = None
        self.identity: RNS.Identity | None = None
        self.destination: RNS.Destination | None = None

        # Message handler: called with (source_hash, content, profile)
        self._message_handler = None
        self._running = False

        # Channel profiler for adaptive responses
        self.profiler = ChannelProfiler()

        # Thread pool for non-blocking message processing
        self._pool: ThreadPoolExecutor | None = None

    @property
    def address(self) -> str | None:
        """The LXMF address (hex hash) of this bridge, or None if not started."""
        if self.destination:
            return RNS.prettyhexrep(self.destination.hash)
        return None

    # ── Step-through ("steps") mode ─────────────────────────────────

    step_through = StepThroughManager()

    @property
    def _hold_state_path(self) -> Path:
        return Path(HOLD_STATE_PATH)

    def push_reply(
        self,
        recipient_hex: str,
        text: str,
        source_identity=None,
        chunk: bool = True,
        max_chars: int = STEP_CHUNK_CHARS,
    ) -> bool:
        """
        Push a proactive (non-reply) LXMF message, optionally split into
        multiple posts for long content.

        This is how step-through mode delivers FULL tool calls and full
        tool output over mesh: the text is split into ≤max_chars pieces
        (numbered [n/N]) and sent as separate LXMF messages, with a small
        delay between parts for the LoRa profiles.  Bandwidth cost is
        user-approved.

        Returns True if at least one part was dispatched.
        """
        if not text:
            return False
        parts = split_message(text, max_chars) if chunk else [text]
        if len(parts) > 1:
            logger.info(
                "Push reply to %s: %d chars → %d parts (max=%d)",
                recipient_hex[:16], len(text), len(parts), max_chars,
            )
        ok = False
        for i, part in enumerate(parts):
            if i > 0:
                # LoRa profiles get a pause between posts; TCP doesn't care
                # but 500ms is harmless and keeps ordering clean.
                time.sleep(0.5)
            if self.send_reply(recipient_hex, part, source_identity):
                ok = True
        return ok

    def set_message_handler(self, handler):
        """
        Register the handler called for each incoming message.

        The handler receives (source_hash_hex, message_content) and should
        return a reply string (or None for no reply).
        """
        self._message_handler = handler

    def start(self):
        """
        Initialize RNS, create the LXM Router, register identity,
        and start listening for messages.
        """
        logger.info("Starting Hermes for Reticulum bridge...")

        # Ensure storage directory exists
        self.storage_path.mkdir(parents=True, exist_ok=True)

        # Initialize Reticulum
        self.reticulum = RNS.Reticulum(self.rns_config_path)
        logger.info("Reticulum initialized")

        # Create LXM Router
        self.router = LXMF.LXMRouter(
            storagepath=str(self.storage_path),
            enforce_stamps=self.enforce_stamps,
        )

        # Load or create identity
        identity_path = self.storage_path / "hermes_identity"
        if identity_path.exists():
            self.identity = RNS.Identity.from_file(str(identity_path))
            if self.identity is None:
                logger.warning("Corrupt identity at %s, creating new one", identity_path)
                self.identity = RNS.Identity()
                self.identity.to_file(str(identity_path))
            else:
                logger.info("Loaded existing identity from %s", identity_path)
        else:
            self.identity = RNS.Identity()
            self.identity.to_file(str(identity_path))
            logger.info("Created new identity at %s", identity_path)

        # Register delivery identity and destination
        self.destination = self.router.register_delivery_identity(
            self.identity,
            display_name=self.display_name,
            stamp_cost=self.stamp_cost,
        )

        # Register the inbound message callback
        self.router.register_delivery_callback(self._on_lxmf_message)

        # Create thread pool for message processing
        self._pool = ThreadPoolExecutor(
            max_workers=_MAX_HANDLERS,
            thread_name_prefix="lxmf-handler",
        )

        self._running = True

        logger.info(
            "Bridge ready — LXMF address: %s | Display name: %s",
            self.address,
            self.display_name,
        )

    def announce(self):
        """Announce our destination on the Reticulum network."""
        if self.destination:
            self.destination.announce()
            logger.info("Announced destination %s", self.address)

    def _on_lxmf_message(self, message):
        """
        Internal callback for incoming LXMF messages.

        This runs inside the RNS event loop thread. To avoid blocking
        the mesh stack, we dispatch the actual processing to a thread pool.
        """
        try:
            # Extract message content (fast, safe to do here)
            if hasattr(message, "content_as_string"):
                content = message.content_as_string()
            else:
                content = str(message.content)

            source_hash = RNS.prettyhexrep(message.source_hash)
            src_bytes = message.source_hash
            source_hash_raw = (
                src_bytes.hex() if isinstance(src_bytes, bytes) else src_bytes.hex()
            )

            # Log reception
            sig = "valid" if message.signature_validated else "invalid/unknown"
            method_name = {
                LXMF.LXMessage.OPPORTUNISTIC: "opportunistic",
                LXMF.LXMessage.DIRECT: "link",
                LXMF.LXMessage.PROPAGATED: "propagated",
            }.get(getattr(message, "method", None), "unknown")

            logger.info(
                "Received LXMF from %s [%s, sig=%s]: %.100s",
                source_hash, method_name, sig, content,
            )

            # Extract channel metrics and classify
            metrics = ChannelMetrics.from_lxmessage(message)
            profile = self.profiler.classify(metrics)

            # Dispatch to thread pool (non-blocking)
            if self._message_handler and self._pool:
                # Pass source identity for reply routing
                source_identity = getattr(message, "source", None)
                self._pool.submit(
                    self._process_and_reply, source_hash_raw, content, profile, source_identity,
                )
            else:
                logger.warning("No handler or pool — dropping from %s", source_hash)

        except Exception as e:
            logger.error("Error in LXMF callback: %s", e, exc_info=True)

    def _process_and_reply(
        self, source_hash: str, content: str, profile=None, source_identity=None,
    ):
        """
        Process a message and send the reply. Runs in a thread pool worker.
        """
        try:
            reply = self._message_handler(source_hash, content, profile)
            if reply:
                # Adaptive truncation and splitting
                parts = prepare_reply(reply, profile)
                for i, part in enumerate(parts):
                    if i > 0 and profile:
                        time.sleep(profile.send_delay_ms / 1000)
                    self.send_reply(source_hash, part, source_identity)
        except Exception as e:
            logger.error(
                "Error processing message from %s: %s",
                source_hash[:16], e, exc_info=True,
            )

    def send_reply(self, recipient_hex: str, text: str, source_identity=None) -> bool:
        """
        Send an LXMF text message to a recipient.

        Args:
            recipient_hex: Hex string of the recipient's LXMF hash.
            text: Message content (plain text).
            source_identity: The sender's RNS.Identity (from the incoming message).
                If provided, used directly instead of recalling from store.

        Returns:
            True if the message was dispatched, False on error.
        """
        if not self.router or not self.destination:
            logger.error("Bridge not started — cannot send reply")
            return False

        # Use provided identity or try to recall
        # source_identity may be an RNS.Destination (from message.source)
        # or an RNS.Identity — extract the Identity in either case
        recipient_identity = source_identity
        if recipient_identity is not None:
            # If it's a Destination, extract the underlying Identity
            if hasattr(recipient_identity, "identity"):
                recipient_identity = recipient_identity.identity
        if recipient_identity is None:
            try:
                recipient_hash = bytes.fromhex(recipient_hex)
            except ValueError:
                logger.error("Invalid recipient hash: %s", recipient_hex)
                return False

            # Try recall first
            recipient_identity = RNS.Identity.recall(recipient_hash)

            # If unknown, request path to trigger identity exchange
            if recipient_identity is None:
                logger.info(
                    "Identity unknown for %s — requesting path...",
                    recipient_hex[:16],
                )
                RNS.Transport.request_path(recipient_hash)
                # Poll for identity arrival (up to 8s)
                for _ in range(8):
                    time.sleep(1)
                    recipient_identity = RNS.Identity.recall(recipient_hash)
                    if recipient_identity is not None:
                        break

        if recipient_identity is None:
            logger.error(
                "Unknown recipient identity for %s — cannot send",
                recipient_hex,
            )
            return False

        try:
            # Build the destination for the recipient
            dest = RNS.Destination(
                recipient_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )

            # Create and dispatch the LXMF message
            lxm = LXMF.LXMessage(
                dest,
                self.destination,
                text,
                desired_method=LXMF.LXMessage.DIRECT,
                include_ticket=True,
            )
            self.router.handle_outbound(lxm)

            logger.info(
                "Reply dispatched to %s (%d bytes)",
                recipient_hex[:16], len(text.encode("utf-8")),
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to send reply to %s: %s",
                recipient_hex[:16], e, exc_info=True,
            )
            return False

    def stop(self):
        """Gracefully shut down the bridge."""
        logger.info("Shutting down Hermes for Reticulum bridge...")
        self._running = False

        if self._pool:
            self._pool.shutdown(wait=False)

    def _clean_exit(self):
        # RNS C-level event-loop threads outlive the Python loop; force-exit after stop().
        try:
            self.stop()
            RNS.exit(0)
        except Exception:
            os._exit(0)

    def run_forever(self):
        """Start the bridge and block until interrupted."""
        self.start()
        self.announce()

        # Signal handler returns immediately; teardown runs on a daemon thread
        # so RNS.exit() can't deadlock against the C event loop holding the GIL.
        def _handle_signal(signum, frame):
            logger.info("Signal %s received, shutting down...", signum)
            self._running = False
            threading.Thread(target=self._clean_exit, daemon=True).start()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        logger.info("Bridge running. Press Ctrl+C to stop.")

        try:
            while self._running:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self.stop()
