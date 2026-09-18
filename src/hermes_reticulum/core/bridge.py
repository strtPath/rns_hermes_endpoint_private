"""LXMF Message Bridge — connects Reticulum/LXMF to Hermes Agent."""

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
from hermes_reticulum.core.bridge_liveness import BridgeLiveness
from hermes_reticulum.core.downlink import (
    DownlinkTracker,
    MIN_CHUNK_INTERVAL_MS,
    sequence_chunks,
)
from hermes_reticulum.core.profiler import ChannelMetrics, ChannelProfiler
from hermes_reticulum.utils import expand_path

logger = logging.getLogger("hermes_reticulum.bridge")

# Max concurrent message handlers — prevents hermes subprocess flood.
_MAX_HANDLERS = 4

# Chunked tool I/O delivery: cap per LXMF post (1500 ≈ 4 × ~368-byte blocks).
STEP_CHUNK_CHARS = 1500

# Step-through checkpoint gate state (bridge-side; hook polls via files).
_step_mode_var: ContextVar[str] = ContextVar("reticulum_step_mode", default="off")
_hold_release_var: ContextVar[bool] = ContextVar("reticulum_hold_release", default=False)
_hold_active_var: ContextVar[bool] = ContextVar("reticulum_hold_active", default=False)

# Hold timeout: release if user never says /go. 0 = hold indefinitely.
HOLD_TIMEOUT_S = float(os.environ.get("HERMES_STEP_HOLD_TIMEOUT", "1800"))

# Periodic re-announce cadence (minutes). RNS drops destinations from other
# nodes' path tables over time, so a long-lived bridge must re-announce to stay
# discoverable. 0 = disable periodic re-announce (announce only at startup).
# Change by editing RETICULUM_ANNOUNCE_INTERVAL in .env and restarting the bridge.
# (Clamped to MIN_ANNOUNCE_INTERVAL_MIN in ReticulumBridge.__init__ when positive.)

# Floor for the live /announce <minutes> override and the env default: below
# this the re-announce broadcast becomes a spam loop that burns bridge and
# mesh bandwidth.
MIN_ANNOUNCE_INTERVAL_MIN = 1.0


HOLD_STATE_PATH = os.environ.get(
    "HERMES_STEP_HOLD_FILE",
    os.path.expanduser("~/.hermes/.reticulum-hold-state"),
)


MODE_STATE_PATH = os.environ.get(
    "HERMES_STEP_MODE_FILE",
    os.path.expanduser("~/.hermes/.reticulum-step-mode"),
)


class StepThroughManager:
    """Step-through mode: print full tool call + output before model moves on."""

    def __init__(self):
        self._lock = threading.Lock()
        self.enabled = False
        self.hold_requested = False


    def set_mode(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = bool(enabled)

        try:
            Path(MODE_STATE_PATH).write_text("1" if enabled else "0")
        except OSError as e:
            logger.debug("Could not write mode state file: %s", e)

    def is_enabled(self) -> bool:
        with self._lock:
            return self.enabled

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
        """Block until hold released (or timeout). Only blocks if /hold was requested."""
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
        self.storage_path = Path(
            expand_path(storage_path or "~/.lxmf/storage")
        )
        self.stamp_cost = stamp_cost
        self.enforce_stamps = enforce_stamps
        self.rns_config_path = str(rns_config_path) if rns_config_path else None

        self.reticulum: RNS.Reticulum | None = None
        self.router: LXMF.LXMRouter | None = None
        self.identity: RNS.Identity | None = None
        self.destination: RNS.Destination | None = None

        self._message_handler = None
        self._running = False

        self.profiler = ChannelProfiler()
        self.downlink = DownlinkTracker()

        self._pool: ThreadPoolExecutor | None = None

        # Bridge-level liveness proxy (Tier 4.5). Started in run_forever()
        # after start()/announce(); probes RNS and pings systemd's watchdog.
        self.liveness: BridgeLiveness | None = None

        # Periodic re-announce scheduler (see announce()). Started by
        # announce(); stopped by stop().
        # env_announce_interval_min is the RETICULUM_ANNOUNCE_INTERVAL value as
        # set in .env (the true "default"). Clamp sub-floor positive values to
        # the floor so a startup announce() (which uses this when interval_min
        # is omitted) can't trip the MIN_ANNOUNCE_INTERVAL_MIN guard; 0 =
        # disable is passed through unchanged.
        env_interval = float(os.environ.get("RETICULUM_ANNOUNCE_INTERVAL", "30"))
        if 0 < env_interval < MIN_ANNOUNCE_INTERVAL_MIN:
            env_interval = MIN_ANNOUNCE_INTERVAL_MIN
        self.env_announce_interval_min: float = env_interval
        self.announce_interval_min: float = env_interval
        self._announce_timer: threading.Thread | None = None
        self._announce_timer_stop: threading.Event | None = None
        # Serializes the (stop-old / start-new) timer swap in announce() so
        # concurrent /announce commands can't join an unstarted thread.
        self._announce_lock = threading.Lock()

    @property
    def address(self) -> str | None:
        """The LXMF address (hex hash) of this bridge, or None if not started."""
        if self.destination:
            return RNS.prettyhexrep(self.destination.hash)
        return None


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
        """Push a proactive LXMF message, optionally split into multiple posts.

        Multi-part pushes are tagged with ``[p<N> i/N]`` so recipients can
        spot a dropped tail without protocol changes. Pacing is enforced
        between successful sends (a failed send does not consume budget).
        """
        if not text:
            return False
        parts = split_message(text, max_chars) if chunk else [text]
        if len(parts) > 1:
            logger.info(
                "Push reply to %s: %d chars → %d parts (max=%d)",
                recipient_hex[:16], len(text), len(parts), max_chars,
            )
            tag = self.downlink.next_push_tag()
            parts = sequence_chunks(parts, tag)
        ok = False
        for i, part in enumerate(parts):
            if i > 0:
                # Pace *before* the send; record_send inside send_reply
                # updates the clock only on success.
                self.downlink.pace_wait(recipient_hex, MIN_CHUNK_INTERVAL_MS)
            if self.send_reply(recipient_hex, part, source_identity):
                ok = True
        return ok

    def set_message_handler(self, handler):
        """Register the handler called for each incoming message."""
        self._message_handler = handler

    def start(self):
        """Initialize RNS, create the LXM Router, register identity."""
        logger.info("Starting Hermes for Reticulum bridge...")

        self.storage_path.mkdir(parents=True, exist_ok=True)

        self.reticulum = RNS.Reticulum(self.rns_config_path)
        logger.info("Reticulum initialized")


        self.router = LXMF.LXMRouter(
            storagepath=str(self.storage_path),
            enforce_stamps=self.enforce_stamps,
        )

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

        self.destination = self.router.register_delivery_identity(
            self.identity,
            display_name=self.display_name,
            stamp_cost=self.stamp_cost,
        )

        self.router.register_delivery_callback(self._on_lxmf_message)

        # Freeze the delivery hash at registration. RNS.Destination.hash is
        # computed dynamically and can drift if RNS internals (transport
        # registration, ratchet context) change after __init__; capturing it
        # here means /announce and the periodic re-announce always reference
        # the exact destination that was registered (see the 2026-09-15
        # identity/announce hash-drift findings).
        self._delivery_hash = self.destination.hash

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

    def announce(self, interval_min: float = None):
        """Announce our destination on the Reticulum network.

        Also (re)starts the periodic re-announce timer. Pass ``interval_min``
        to override the env-configured cadence for this run (0 disables).
        When omitted, the current live cadence is kept (so a bare re-announce
        preserves a /announce <min> override). Values below
        ``MIN_ANNOUNCE_INTERVAL_MIN`` (except 0 = disable) are rejected to
        avoid flooding the mesh. The first announce fires immediately;
        subsequent ones fire every ``interval_min`` minutes while the bridge
        runs.
        """
        if interval_min is None:
            # Keep the current live cadence (preserves a /announce <min>
            # override instead of snapping back to the env default).
            interval_min = self.announce_interval_min
        if 0 < interval_min < MIN_ANNOUNCE_INTERVAL_MIN:
            raise ValueError(
                f"Interval too small: {interval_min} min "
                f"(minimum {MIN_ANNOUNCE_INTERVAL_MIN} min to avoid spamming the mesh)"
            )

        self.announce_interval_min = interval_min

        if self.destination:
            self._do_announce()

        with self._announce_lock:
            # If stop() already ran and cleared the timer, don't spawn a new
            # daemon thread after shutdown — it would run past the bridge
            # lifecycle (the loop only notices _running=False at its next tick).
            if not self._running:
                logger.debug("announce(): bridge stopped; skipping timer start")
                return
            if interval_min and interval_min > 0:
                self._start_announce_timer(interval_min)
                logger.info(
                    "Periodic re-announce every %.0f min (address %s)",
                    interval_min, self.address,
                )
            else:
                self._announce_timer = None
                self._announce_timer_stop = None
                logger.info("Periodic re-announce disabled (interval=0)")

    def _do_announce(self):
        """Send the actual RNS announce for the registered destination."""
        if self.destination:
            self.destination.announce()
            logger.info("Announced destination %s", self.address)

    def _start_announce_timer(self, interval_min: float):
        """(Re)start the periodic re-announce scheduler thread."""
        self._stop_announce_timer()
        self._announce_timer_stop = threading.Event()
        interval_s = interval_min * 60
        self._announce_timer = threading.Thread(
            target=self._announce_loop,
            args=(interval_s,),
            name="lxmf-announce-timer",
            daemon=True,
        )
        self._announce_timer.start()

    def _announce_loop(self, interval_s: float):
        """Fire a re-announce every interval_s until the bridge stops."""
        stop = self._announce_timer_stop
        while not stop.wait(interval_s):
            if not self._running:
                break
            try:
                self._do_announce()
            except Exception as e:
                logger.warning("Periodic re-announce failed: %s", e)

    def _stop_announce_timer(self):
        """Stop the periodic re-announce thread, if running."""
        if self._announce_timer_stop is not None:
            self._announce_timer_stop.set()
        if self._announce_timer is not None:
            self._announce_timer.join(timeout=5)
        self._announce_timer = None

    def _on_lxmf_message(self, message):
        """Callback for incoming LXMF messages (runs in RNS event loop thread)."""
        try:
            if hasattr(message, "content_as_string"):
                content = message.content_as_string()
            else:
                content = str(message.content)

            source_hash = RNS.prettyhexrep(message.source_hash)
            src_bytes = message.source_hash
            source_hash_raw = (
                src_bytes.hex() if isinstance(src_bytes, bytes) else src_bytes.hex()
            )

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

            metrics = ChannelMetrics.from_lxmessage(message)
            profile = self.profiler.classify(metrics)

            if self._message_handler and self._pool:
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
        """Process a message and send the reply (runs in thread pool worker)."""
        try:
            reply = self._message_handler(source_hash, content, profile)
            if reply:
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
        """Send an LXMF text message to a recipient. Returns True if dispatched.

        Registers a per-chunk delivery callback so we can observe first-hop
        acks (DELIVERED) vs propagation (SENT) vs silent loss (timeout sweep
        in DownlinkTracker). A failed send does NOT advance the pacing clock
        (record_send is only called after a successful handle_outbound).
        """
        if not self.router or not self.destination:
            logger.error("Bridge not started — cannot send reply")
            return False

        # source_identity may be RNS.Destination or RNS.Identity — extract Identity.
        recipient_identity = source_identity
        if recipient_identity is not None:
            if hasattr(recipient_identity, "identity"):
                recipient_identity = recipient_identity.identity
        if recipient_identity is None:
            try:
                recipient_hash = bytes.fromhex(recipient_hex)
            except ValueError:
                logger.error("Invalid recipient hash: %s", recipient_hex)
                return False

            recipient_identity = RNS.Identity.recall(recipient_hash)

            if recipient_identity is None:
                logger.info(
                    "Identity unknown for %s — requesting path...",
                    recipient_hex[:16],
                )
                RNS.Transport.request_path(recipient_hash)

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

        # Allocate the seq BEFORE the send so the dispatch time is recorded
        # even if the callback never fires (silent loss → timeout sweep).
        seq = self.downlink.next_seq()

        try:
            dest = RNS.Destination(
                recipient_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )

            lxm = LXMF.LXMessage(
                dest,
                self.destination,
                text,
                desired_method=LXMF.LXMessage.DIRECT,
                include_ticket=True,
            )
            lxm.register_delivery_callback(
                lambda msg, _s=seq, _r=recipient_hex: self._on_outbound(_s, _r, msg)
            )
            self.router.handle_outbound(lxm)

            # Success: record the pacing clock and the recipient for the seq.
            self.downlink.record_send(recipient_hex)
            self.downlink.register_dispatch(seq, recipient_hex)

            logger.info(
                "Reply dispatched to %s seq=%d (%d bytes)",
                recipient_hex[:16], seq, len(text.encode("utf-8")),
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to send reply to %s: %s",
                recipient_hex[:16], e, exc_info=True,
            )
            return False

    def _on_outbound(self, seq: int, recipient_hex: str, lxm) -> None:
        """LXMF delivery callback (fires on the RNS event-loop thread).

        Maps LXMessage state → outcome, logs the ack, and records it in the
        tracker. Idempotent: a second invocation (delivery receipt +
        propagation receipt, or a re-queue) is a no-op on the counter
        (seq already popped by the first note_outcome).
        """
        try:
            state = getattr(lxm, "state", None)
            from hermes_reticulum.core.downlink import _state_name, _state_outcome
            state_str = _state_name(state) if state is not None else "none"
            outcome = _state_outcome(state) if state is not None else "unknown"
            self.downlink.note_outcome(seq, outcome)
            logger.info(
                "Downlink ack seq=%d → %s state=%s (%s)",
                seq, recipient_hex[:16], outcome, state_str,
            )
        except Exception as e:
            logger.debug("Downlink callback error: %s", e)

    def stop(self):
        """Gracefully shut down the bridge."""
        logger.info("Shutting down Hermes for Reticulum bridge...")
        self._running = False

        # Same lock as announce() so stop() can't join a timer that a
        # concurrent /announce is mid-creation.
        with self._announce_lock:
            self._stop_announce_timer()

        if self.liveness:
            self.liveness.stop()

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

        # Liveness proxy (Tier 4.5): probes RNS and pings systemd's
        # watchdog. Started after announce() so the bridge is fully up
        # before the first READY/WATCHDOG.
        self.liveness = BridgeLiveness()
        self.liveness.start()

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
