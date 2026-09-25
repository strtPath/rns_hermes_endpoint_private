"""
LXMF-backed transport for the Reticulum gateway platform adapter.

Implements the ``Transport`` protocol declared in ``adapter.py`` on top of
a real RNS/LXMF stack (RNS 1.5.x / LXMF 1.1.x):

- ``send_to`` builds an ``LXMessage`` and hands it to the router; a False
  return (no interface could carry the packet) is a normal result, never
  an exception (spec section 6: transient failure).
- Delivery / failure callbacks fire on the RNS thread; the adapter already
  hops threads with ``loop.call_soon_threadsafe``, so this module makes
  plain calls and owns no asyncio state (spec section 5).
- Announce cadence: the periodic re-announce is driven by RNS's own
  announce machinery; this module validates the configured interval
  against the bridge's hard-won rules (``core/bridge.py``) — 0 disables,
  below-floor is rejected, nan/inf are rejected — and fails loudly at
  startup rather than substituting a cadence (spec section 11).
- Shared-instance mode (spec section 13): RNS owns reconnect internally
  (LocalInterface auto-reconnects), so there is NO reattach/polling/
  supervision loop here — the transport is simply restartable and logs
  the final instance state (with the RNS version, which a shared
  instance couples by start order with no warning).

Configuration is read from ``PlatformConfig.extra`` first, with the
scoped env reader as fallback — never ``os.getenv`` (spec section 16:
under multiplexing a raw env read silently returns the default
profile's value).
"""

import logging
import math
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import LXMF
import RNS
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret

from hermes_reticulum.plugin import delivery
from hermes_reticulum.utils import expand_path

logger = logging.getLogger("hermes_reticulum.transport")

# Floor for the periodic re-announce cadence (minutes), matching
# core/bridge.py: below this the re-announce broadcast becomes a spam
# loop that burns mesh bandwidth. 0 is legal and means "disable
# periodic re-announce" (receive-only nodes).
MIN_ANNOUNCE_INTERVAL_MIN = 1.0

# The spec default is 60 minutes (the bridge's standalone default is 30;
# do not copy it — a gateway-connected node is re-announced more rarely
# and must not spam a shared mesh).
DEFAULT_ANNOUNCE_INTERVAL_MIN = 60.0

# Propagation node policy. "auto" lets LXMF autopeer with a node it discovers;
# a 32-hex destination hash pins one explicitly (a user's own node, or one they
# trust). "off" disables propagated delivery entirely, so a pathless reply fails
# instead of parking somewhere.
PROPAGATION_MODE_AUTO = "auto"
PROPAGATION_MODE_OFF = "off"
PROPAGATION_MODE_PINNED = "pinned"


def validate_propagation_node(
    value: Any, source: str = "propagation node"
) -> tuple[str, str | None]:
    """Parse the propagation setting into (mode, destination_hash).

    Accepted forms:

    - ``auto`` (or empty/unset) -> autopeer, LXMF's own default behaviour.
    - ``off`` -> never propagate; a pathless reply fails honestly.
    - a 32-character hex destination hash -> pin that node.

    Anything else raises, at startup, rather than silently falling back to a
    different delivery policy: this setting decides whether a reply can reach a
    phone that is offline, so a typo must not quietly change the answer.
    """
    if value is None:
        return PROPAGATION_MODE_AUTO, None
    text = str(value).strip()
    if not text:
        return PROPAGATION_MODE_AUTO, None
    lowered = text.lower()
    if lowered == PROPAGATION_MODE_AUTO:
        return PROPAGATION_MODE_AUTO, None
    if lowered == PROPAGATION_MODE_OFF:
        return PROPAGATION_MODE_OFF, None
    if len(lowered) != 32 or any(c not in "0123456789abcdef" for c in lowered):
        raise ValueError(
            f"Invalid {source}={value!r}: expected 'auto', 'off', or a "
            f"32-character hex destination hash."
        )
    return PROPAGATION_MODE_PINNED, lowered


def validate_announce_interval(value: Any, source: str = "announce interval") -> float:
    """Validate an announce interval in minutes.

    Accepted: a finite number >= 0, where 0 disables periodic
    re-announce. Rejected (ValueError, at startup): non-numeric input,
    negatives, values strictly between 0 and MIN_ANNOUNCE_INTERVAL_MIN,
    and nan/inf/-inf (these pass a naive range check — every comparison
    with nan is False — and then break the scheduler).

    This mirrors the bridge's hard-won validation (core/bridge.py):
    invalid values fail loudly instead of silently substituting a
    different cadence.
    """
    try:
        interval = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid {source}={value!r}: expected a number of minutes "
            f"(e.g. 1, 60; 0 disables periodic re-announce)."
        ) from None
    if not math.isfinite(interval) or interval < 0:
        raise ValueError(
            f"Invalid {source}={value!r}: must be a finite number >= 0 "
            f"(nan/inf are not valid cadences)."
        )
    if 0 < interval < MIN_ANNOUNCE_INTERVAL_MIN:
        raise ValueError(
            f"{source}={value!r} min is below the minimum "
            f"{MIN_ANNOUNCE_INTERVAL_MIN:g} min — that cadence would spam "
            f"the mesh. Use 0 to disable, or at least "
            f"{MIN_ANNOUNCE_INTERVAL_MIN:g} minutes."
        )
    return interval


class ReticulumTransport:
    """Real LXMF-backed implementation of the ``Transport`` protocol."""

    def __init__(
        self,
        display_name: str = "Hermes for Reticulum",
        announce_interval_min: Any = DEFAULT_ANNOUNCE_INTERVAL_MIN,
        storage_path: str | None = None,
        rns_config_path: str | None = None,
        require_shared_instance: bool | None = None,
        extra: dict | None = None,
    ):
        """Build the transport. ``extra`` (the adapter's PlatformConfig.extra)
        takes precedence; missing settings fall back to the scoped env
        reader, then to defaults. Raises ValueError on an invalid
        announce interval (fail loudly at startup)."""
        extra = dict(extra or {})
        self._name_lock = threading.Lock()
        with self._name_lock:
            self._display_name = (
                extra.get("display_name")
                or _get_scoped_secret("RETICULUM_DISPLAY_NAME")
                or "Hermes for Reticulum"
            )
        raw_interval = extra.get("announce_interval")
        if raw_interval is None:
            raw_interval = _get_scoped_secret("RETICULUM_ANNOUNCE_INTERVAL")
        if raw_interval is None:
            raw_interval = DEFAULT_ANNOUNCE_INTERVAL_MIN
        self.announce_interval_min = validate_announce_interval(
            raw_interval, "RETICULUM_ANNOUNCE_INTERVAL"
        )

        self.storage_path = expand_path(
            extra.get("storage_path")
            or _get_scoped_secret("RETICULUM_STORAGE_PATH")
            or "~/.hermes/.reticulum-gateway/storage"
        )
        self.rns_config_path = (
            str(extra.get("rns_config_path")
                or _get_scoped_secret("RETICULUM_RNS_CONFIG_PATH"))
            if (extra.get("rns_config_path")
                or _get_scoped_secret("RETICULUM_RNS_CONFIG_PATH"))
            else None
        )
        # Propagation policy: who carries a reply when the peer has no path.
        # "auto" autopeers, "off" disables, or a 32-hex hash pins one node.
        raw_propagation = extra.get("propagation_node")
        if raw_propagation is None:
            raw_propagation = _get_scoped_secret("RETICULUM_PROPAGATION_NODE")
        self.propagation_mode, self.propagation_node_hash = (
            validate_propagation_node(raw_propagation, "RETICULUM_PROPAGATION_NODE")
        )
        # True until a send proves otherwise: the adapter reads this to pace
        # chunks, and a first chunk should never be delayed on a guess.
        self._last_send_direct = True
        # Shared-instance mode is operator policy, not env state: take it
        # only from the explicit argument or PlatformConfig.extra.
        self.require_shared_instance = bool(
            require_shared_instance
            if require_shared_instance is not None
            else bool(extra.get("require_shared_instance", False))
        )

        self.reticulum: RNS.Reticulum | None = None
        self.router: LXMF.LXMRouter | None = None
        self.identity: RNS.Identity | None = None
        self.destination: RNS.Destination | None = None
        self._started = False

        # Per-message delivery/failure callbacks registered through the
        # protocol. LXMF fires the per-message callback with the message
        # itself, so we wrap each registration.
        self._inbound_cb: Callable | None = None
        self._outbound_cb: Callable | None = None
        self._failed_cb: Callable | None = None

    # ── Display name ───────────────────────────────────────────────────

    @property
    def display_name(self) -> str:
        with self._name_lock:
            return self._display_name

    def set_display_name(self, name: str) -> None:
        """Change the announced display name at runtime, no restart.

        The destination's default app data is a callable invoked on every
        announce (RNS.Destination.set_default_app_data), so the next
        re-announce carries the new name.
        """
        name = str(name or "").strip() or self.display_name
        with self._name_lock:
            self._display_name = name
        destination = self.destination
        if destination is not None:
            destination.set_default_app_data(lambda: name.encode("utf-8"))
        logger.info("Display name changed to %s (takes effect on next announce)", name)

    # ── Lifecycle ──────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._started

    def start(self) -> None:
        """Start the RNS instance, the LXMF router, and our delivery
        identity. Idempotent while up; a fresh start after ``stop()``
        re-initialises from scratch. No reattach/supervision loop: RNS
        owns reconnect internally (spec section 13)."""
        if self._started:
            logger.info("Transport already started (idempotent start)")
            return

        os.makedirs(self.storage_path, exist_ok=True)

        reticulum_kwargs = {}
        if self.rns_config_path:
            reticulum_kwargs["configdir"] = self.rns_config_path
        if self.require_shared_instance:
            reticulum_kwargs["require_shared_instance"] = True

        self.reticulum = RNS.Reticulum(**reticulum_kwargs)
        self._log_instance_state()

        # No identity argument: RNS 1.5.4's Reticulum exposes no identity to
        # hand over (there is no `internal_identity`), and LXMRouter takes the
        # delivery identity at register_delivery_identity below. This matches
        # how core/bridge.py constructs the router in production.
        self.router = LXMF.LXMRouter(
            storagepath=str(self.storage_path),
        )
        self._apply_propagation_policy()

        self.identity = self._load_or_create_identity(
            f"{self.storage_path}/gateway_identity"
        )

        self.destination = self.router.register_delivery_identity(
            self.identity,
            display_name=self.display_name,
        )
        if self.destination is None:
            raise RuntimeError(
                "register_delivery_identity returned None — a delivery "
                "identity is already registered with this router instance"
            )

        # The callable form: invoked on every announce, so a runtime
        # set_display_name() is picked up without a restart.
        self.destination.set_default_app_data(lambda: self.display_name.encode("utf-8"))

        # Router-level inbound delivery callback: forward incoming
        # messages (the adapter drains them into its queue).
        self.router.register_delivery_callback(self._on_router_delivery)

        # The transport thread is the stack lifecycle, and only the process
        # that OWNS the RNS instance may start it. Attached to a shared
        # instance the daemon already runs Transport, so starting it here
        # re-registers its internal destinations and raises KeyError. The
        # production bridge never starts it, for the same reason (spec
        # section 11: do not fight the shared instance).
        if not self.reticulum.is_connected_to_shared_instance:
            RNS.Transport.start(self.reticulum)
        else:
            logger.info(
                "Attached to a shared RNS instance; the daemon owns the "
                "transport thread, so it is not started here"
            )

        self._started = True
        logger.info(
            "LXMF transport started (display name %s, announce every %g min)",
            self.display_name,
            self.announce_interval_min if self.announce_interval_min > 0 else 0,
        )

    def _apply_propagation_policy(self) -> None:
        """Configure the router's outbound propagation node.

        Three modes, from ``RETICULUM_PROPAGATION_NODE`` / PlatformConfig.extra:

        - ``auto``: leave LXMF's own autopeering alone. It peers with nodes it
          discovers, within ``AUTOPEER_MAXDEPTH`` (4) hops.
        - ``off``: disable autopeering and clear any pinned node, so a reply
          with no path fails instead of parking on a third party.
        - a 32-hex hash: pin that node explicitly. Needed for a self-hosted or
          trusted node, because autopeering only sees nodes that happen to
          sync with us and gives no say over which one carries our traffic.

        A pinned hash whose identity is not yet known is not fatal: the node's
        announce populates it, and ``set_outbound_propagation_node`` accepts the
        hash regardless. We log the current knowledge so a misconfigured hash
        is visible rather than silent.
        """
        router = self.router
        if router is None:
            return

        if self.propagation_mode == PROPAGATION_MODE_OFF:
            router.autopeer = False
            try:
                router.set_outbound_propagation_node(None)
            except Exception as e:
                logger.debug("Clearing propagation node failed: %s", e)
            logger.info(
                "Propagation disabled (RETICULUM_PROPAGATION_NODE=off); a reply "
                "with no path to the peer will fail rather than be propagated"
            )
            return

        if self.propagation_mode == PROPAGATION_MODE_AUTO:
            logger.info(
                "Propagation node: auto (LXMF autopeering, max depth %s)",
                getattr(router, "autopeer_maxdepth", "?"),
            )
            return

        dest_hash = bytes.fromhex(self.propagation_node_hash or "")
        try:
            router.set_outbound_propagation_node(dest_hash)
        except Exception as e:
            logger.error(
                "Could not pin propagation node %s: %s",
                (self.propagation_node_hash or "")[:8],
                e,
            )
            return
        known = RNS.Identity.recall(dest_hash) is not None
        logger.info(
            "Propagation node pinned to %s.. (identity %s)",
            (self.propagation_node_hash or "")[:8],
            "known" if known else "not yet announced — awaiting its announce",
        )

    def _log_instance_state(self) -> None:
        """Log which kind of RNS instance we landed on, with the RNS
        version in the same line. A shared instance couples versions by
        start order with no warning (spec section 11), so this line must
        answer 'which RNS am I talking to'."""
        r = self.reticulum
        if r.is_shared_instance:
            kind = "shared instance (created this rnsd)"
        elif r.is_connected_to_shared_instance:
            kind = "connected to existing shared instance (rnsd)"
        elif r.is_standalone_instance:
            kind = "standalone instance"
        else:
            kind = "unknown instance state"
        logger.info("Reticulum instance: %s (RNS %s)", kind, RNS.__version__)

    def _load_or_create_identity(self, identity_path: str):
        if os.path.exists(identity_path):
            identity = RNS.Identity.from_file(identity_path)
            if identity is None:
                logger.warning("Corrupt identity at storage path; creating new one")
                identity = RNS.Identity()
                identity.to_file(identity_path)
            else:
                logger.info("Loaded existing identity from storage path")
            return identity
        identity = RNS.Identity()
        identity.to_file(identity_path)
        logger.info("Created new identity at storage path")
        return identity

    def stop(self) -> None:
        """Stop the stack. Safe before start and safe to call twice.

        Teardown mirrors the RNS exit path: the transport stops running
        and persists its data (Transport.exit_handler), and the router is
        dropped so a later ``start()`` re-initialises cleanly.
        """
        if not self._started and self.reticulum is None:
            return
        try:
            RNS.Transport.exit_handler()
        except Exception as e:
            if self.reticulum is not None:
                logger.warning("Transport exit handler failed: %s", e)
            # exit_handler before start() is a no-op by design
        self.reticulum = None
        self.destination = None
        self.identity = None
        self.reticulum = None
        self._started = False
        logger.info("LXMF transport stopped")

    # ── Outbound ───────────────────────────────────────────────────────

    # How long to wait for a path request to answer before giving up on
    # direct delivery. LoRa hops are slow, so this is generous; it only
    # applies when there is no path, so a warm path never pays it.
    PATH_WAIT_SECONDS = 15.0

    # Poll interval inside that wait. Injectable so a test can drive the
    # deadline without sleeping for it (a mocked sleep does not advance
    # time.time, so the loop would otherwise spin for the full real 15s).
    PATH_POLL_SECONDS = 0.5

    def _probe_path(self, dest_hash: bytes) -> tuple[bool, int]:
        """(has_path, hops) for a destination, requesting one if absent.

        DIRECT sends do not request paths (only OPPORTUNISTIC does), so a cold
        router cancels a direct message in about a second
        (``MAX_PATHLESS_TRIES = 1``) even when the peer is reachable and simply
        has not announced recently. Asking first turns that guaranteed
        cancellation into a likely delivery.
        """
        try:
            if RNS.Transport.has_path(dest_hash):
                return True, RNS.Transport.hops_to(dest_hash)
        except Exception as e:
            logger.debug("path probe failed: %s", e)
            return False, 128

        try:
            RNS.Transport.request_path(dest_hash)
        except Exception as e:
            logger.debug("path request failed: %s", e)
            return False, 128

        deadline = time.time() + self.PATH_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(self.PATH_POLL_SECONDS)
            try:
                if RNS.Transport.has_path(dest_hash):
                    hops = RNS.Transport.hops_to(dest_hash)
                    logger.info(
                        "Path to peer appeared after a request (hops=%s)", hops
                    )
                    return True, hops
            except Exception:
                pass
        return False, 128

    def _select_method(self, dest_hash: bytes, has_path: bool) -> int:
        """Pick the LXMF delivery method for this send.

        A path means DIRECT (the whole message travels end to end and we get a
        real delivery receipt). No path means PROPAGATED when a node is
        configured, so the reply parks on the node and reaches a phone that is
        offline -- which is the normal state for a phone. With propagation
        disabled we still try DIRECT, since the router may find a path in the
        meantime; it will fail honestly if not.
        """
        if has_path:
            return LXMF.LXMessage.DIRECT
        if self.propagation_mode == PROPAGATION_MODE_OFF:
            logger.info(
                "No path to peer and propagation is off; attempting direct anyway"
            )
            return LXMF.LXMessage.DIRECT
        node = None
        router = self.router
        if router is not None:
            try:
                node = router.get_outbound_propagation_node()
            except Exception as e:
                logger.debug("could not read outbound propagation node: %s", e)
        if not node:
            logger.warning(
                "No path to peer and no propagation node configured; the message "
                "will fail. Set RETICULUM_PROPAGATION_NODE to 'auto' or a node hash."
            )
            return LXMF.LXMessage.DIRECT
        logger.info("No path to peer; sending via propagation node")
        return LXMF.LXMessage.PROPAGATED

    def last_send_was_direct(self) -> bool:
        """True when the most recent send used a live path (DIRECT).

        The adapter reads this to pace a multi-part reply: direct sends burst,
        propagated sends are spaced. Defaults to True before any send, so the
        first send of a turn is never paced on a guess.
        """
        return self._last_send_direct

    def send_to(self, destination_hash: str, payload: str) -> bool:
        """Send ``payload`` to a destination hash (32 hex chars).

        Returns False (never raises) when the message could not be handed to
        the mesh, or when the router cancels or fails it. A path is probed
        first so the method can be chosen from real reachability rather than
        assumed.
        """
        if not self._started or self.router is None or self.destination is None:
            logger.debug("send_to: transport not started")
            return False
        try:
            dest_hash = bytes.fromhex(destination_hash)
        except ValueError:
            logger.error("send_to: malformed destination hash")
            return False

        try:
            identity = RNS.Identity.recall(dest_hash)
            has_path, hops = self._probe_path(dest_hash)
            if identity is None:
                if not has_path:
                    logger.info(
                        "Peer identity unknown and no path after %.0fs — "
                        "cannot send",
                        self.PATH_WAIT_SECONDS,
                    )
                    return False
                identity = RNS.Identity.recall(dest_hash)
                if identity is None:
                    logger.info("Path exists but identity still unknown — cannot send")
                    return False

            dest = RNS.Destination(
                identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
                LXMF.APP_NAME, "delivery",
            )
            method = self._select_method(dest_hash, has_path)
            self._last_send_direct = method == LXMF.LXMessage.DIRECT
            # Build the message the way the standalone bridge does. There is no
            # LXMRouter.message_for_destination in LXMF — it is LXMessage's
            # constructor — and the router must dispatch it via
            # handle_outbound(), which owns path selection, stamp costing,
            # propagation fallback and retries. Calling lxm.send() directly
            # bypasses all of that.
            lxm = LXMF.LXMessage(
                dest,
                self.destination,
                payload,
                desired_method=method,
                include_ticket=True,
            )
            # Per-message callbacks (they live on LXMessage, not the router).
            # Register BEFORE dispatch so an immediate failure is not missed.
            # These fire on the RNS thread and report OUR OWN message's fate,
            # so they go to the outbound slot -- NOT the inbound one, which
            # would feed our outgoing message back in as peer traffic.
            if self._outbound_cb is not None:
                lxm.register_delivery_callback(self._on_message_outcome)
            if self._failed_cb is not None:
                lxm.register_failed_callback(
                    lambda msg, _cb=self._failed_cb: _cb(self._extract_reason(msg))
                )
            self.router.handle_outbound(lxm)

            # handle_outbound only QUEUES the message: it returns while the
            # router is still deciding whether a route exists. Reporting True
            # here is what made every dropped message look delivered, so give
            # the router a moment to reach a terminal state and report the
            # truth. CANCELLED with no path is the common case this catches.
            cancelled = self._await_outcome(lxm)
            if cancelled:
                logger.warning(
                    "Message to %s.. was cancelled before leaving (hops=%s, "
                    "method=%s) — not delivered",
                    destination_hash[:8],
                    hops,
                    "propagated" if method == LXMF.LXMessage.PROPAGATED else "direct",
                )
                return False
            logger.info(
                "Message handed to the mesh for %s.. (hops=%s, method=%s)",
                destination_hash[:8],
                hops,
                "propagated" if method == LXMF.LXMessage.PROPAGATED else "direct",
            )
            return True
        except Exception as e:
            logger.error("Failed to send to destination: %s", e)
            return False

    # Grace period for the router to reach a terminal state after queueing.
    # A couple of seconds is enough to catch the pathless-cancel
    # (MAX_PATHLESS_TRIES = 1) without holding the caller for a real delivery
    # round trip. Injectable for the same reason as PATH_POLL_SECONDS.
    OUTCOME_GRACE_SECONDS = 2.5
    OUTCOME_POLL_SECONDS = 0.1

    def _await_outcome(self, lxm) -> bool:
        """True when the message reached a terminal failure state.

        LXMF states: GENERATING, OUTBOUND, SENDING, SENT, DELIVERED,
        FAILED, REJECTED, CANCELLED. SENT counts as success — a propagated
        message is complete once the node accepts it. Only FAILED, REJECTED
        and CANCELLED mean the reply did not leave.
        """
        terminal_failure = {
            LXMF.LXMessage.FAILED,
            LXMF.LXMessage.REJECTED,
            LXMF.LXMessage.CANCELLED,
        }
        deadline = time.time() + self.OUTCOME_GRACE_SECONDS
        # Always check at least once: with a zero grace period the loop body
        # would never run, and a message already in a terminal state (the
        # router can cancel synchronously inside handle_outbound) would be
        # reported as delivered.
        while True:
            state = getattr(lxm, "state", None)
            if state in terminal_failure:
                return True
            if state in (LXMF.LXMessage.DELIVERED, LXMF.LXMessage.SENT):
                return False
            if time.time() >= deadline:
                break
            time.sleep(self.OUTCOME_POLL_SECONDS)
        # Still in flight after the grace period: in progress, not failed.
        return False

    @staticmethod
    def _extract_inbound(message) -> tuple:
        """(source_hash, text) for the adapter's drain loop; (None, None)
        to drop. Mirrors the bridge's inbound normalization."""
        try:
            content = (
                message.content_as_string()
                if hasattr(message, "content_as_string")
                else str(message.content)
            )
            src = message.source_hash
            src_hash = (
                src.hex() if isinstance(src, (bytes, bytearray)) else str(src)
            )
            return (src_hash, content)
        except Exception:
            return (None, None)

    @staticmethod
    def _extract_payload(message) -> str:
        """The text of a message WE sent (used for outcome reporting)."""
        content = getattr(message, "content", None)
        if content is None:
            return ""
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return str(content)

    @staticmethod
    def _extract_reason(message) -> str:
        """Human-readable failure reason: the STATE NAME, from the repo's map.

        LXMF exposes state constants but no name function, so the mapping
        lives in ``plugin.delivery`` alongside the state values. Falling back
        to the raw integer keeps a malformed message from raising here.
        """
        state = getattr(message, "state", None)
        try:
            return delivery.state_name(state)
        except Exception:
            return str(state)

    # ── Protocol callback registration ─────────────────────────────────

    def register_inbound_callback(self, callback) -> None:
        """Store the adapter's inbound callback.

        Fired from the router's delivery callback (RNS thread) with
        ``(source_hash, payload)`` when a message is addressed to US.
        """
        self._inbound_cb = callback

    def register_outbound_callback(self, callback) -> None:
        """Store the adapter's outbound-outcome callback.

        Fired from a per-message LXMessage delivery callback (RNS thread) with
        ``(payload, state_name)`` for a message WE sent. Separate from inbound
        because the two carry different things: inbound has a source hash,
        outbound has a terminal state. One slot serving both ran the inbound
        parser over our own outgoing message (2026-09-25 findings).
        """
        self._outbound_cb = callback

    def register_failed_callback(self, callback) -> None:
        """Store the adapter's callback; fired from LXMessage failed
        callbacks (RNS thread) with a human-readable reason string."""
        self._failed_cb = callback

    # ── Router inbound ─────────────────────────────────────────────────

    def _on_router_delivery(self, message) -> None:
        """Router-level delivery callback for messages that reach our
        identity (runs on the RNS thread). Forwards to the stored
        inbound callback; never raises into RNS internals."""
        try:
            if self._inbound_cb is not None:
                self._inbound_cb(*self._extract_inbound(message))
        except Exception as e:
            logger.error("Inbound callback error: %s", e)

    def _on_message_outcome(self, message) -> None:
        """Per-message callback for a message WE sent (RNS thread).

        Reports the terminal state to the outbound slot so a caller can tell
        delivery from mere acceptance, and so a receipt can be matched back to
        the peer it was addressed to.

        Passes the INTEGER state: LXMF's LXMessage has no state-name method
        (only the constants), so the adapter maps it with the repo's own
        ``delivery.state_from_name`` counterpart. Never raises into RNS
        internals.
        """
        try:
            if self._outbound_cb is not None:
                self._outbound_cb(
                    self._extract_payload(message), getattr(message, "state", None)
                )
        except Exception as e:
            logger.error("Outbound outcome callback error: %s", e)
