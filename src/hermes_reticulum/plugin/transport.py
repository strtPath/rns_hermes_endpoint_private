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
        self._delivery_cb: Callable | None = None
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

    def send_to(self, destination_hash: str, payload: str) -> bool:
        """Send ``payload`` to a destination hash (32 hex chars).

        Returns False (never raises) when no interface can carry the
        packet — the transient failure the adapter maps to retryable.
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
            if identity is None:
                # Unknown peer: request a path and poll briefly, as the
                # standalone bridge does.
                RNS.Transport.request_path(dest_hash)
                for _ in range(8):
                    time.sleep(1)
                    identity = RNS.Identity.recall(dest_hash)
                    if identity is not None:
                        break
                if identity is None:
                    logger.info("Identity unknown for destination — cannot send")
                    return False

            dest = RNS.Destination(
                identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
                LXMF.APP_NAME, "delivery",
            )
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
                desired_method=LXMF.LXMessage.DIRECT,
                include_ticket=True,
            )
            # Per-message callbacks (they live on LXMessage, not the router).
            # Register BEFORE dispatch so an immediate failure is not missed.
            # They fire on the RNS thread; the adapter hops threads.
            if self._delivery_cb is not None:
                lxm.register_delivery_callback(
                    lambda msg, _cb=self._delivery_cb: _cb(self._extract_inbound(msg))
                )
            if self._failed_cb is not None:
                lxm.register_failed_callback(
                    lambda msg, _cb=self._failed_cb: _cb(self._extract_reason(msg))
                )
            self.router.handle_outbound(lxm)
            return True
        except Exception as e:
            logger.error("Failed to send to destination: %s", e)
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
    def _extract_reason(message) -> str:
        state = getattr(message, "state", None)
        name = getattr(message, "state", "unknown")
        try:
            return str(LXMF.LXMessage.state_name(state))
        except Exception:
            return str(name)

    # ── Protocol callback registration ─────────────────────────────────

    def register_delivery_callback(self, callback) -> None:
        """Store the adapter's callback; fired from LXMessage delivery
        callbacks (RNS thread) with the inbound (source_hash, text)."""
        self._delivery_cb = callback

    def register_failed_callback(self, callback) -> None:
        """Store the adapter's callback; fired from LXMessage failed
        callbacks (RNS thread) with a human-readable reason string."""
        self._failed_cb = callback

    # ── Router inbound ─────────────────────────────────────────────────

    def _on_router_delivery(self, message) -> None:
        """Router-level delivery callback for messages that reach our
        identity (runs on the RNS thread). Forwards to the stored
        delivery callback; never raises into RNS internals."""
        try:
            if self._delivery_cb is not None:
                self._delivery_cb(self._extract_inbound(message))
        except Exception as e:
            logger.error("Delivery callback error: %s", e)
