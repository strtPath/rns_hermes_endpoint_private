"""
Reticulum Platform Adapter for the Hermes Gateway.

Implements the gateway ``BasePlatformAdapter`` contract so the mesh becomes
a first-class platform: inbound LXMF messages are bridged into the asyncio
event loop and replies go back out over the mesh.

Foundation stage (design: docs/spec-reticulum-platform-adapter.md):

- The transport sits behind the ``Transport`` protocol (spec section 5/10).
  A minimal ``FakeTransport`` in this module proves the wiring without a
  live mesh; the real LXMF implementation is a later ticket and is NOT
  imported here.
- ``connect()`` is idempotent, re-initialises from scratch when
  ``is_reconnect=True``, and returns True when the local transport is up.
- ``send()`` returns a well-formed failing ``SendResult`` for every
  failure class known at this stage; the full DELIVERED/FAILED/SENT receipt
  mapping (spec section 6/14) lands with the real transport.
- Deferred by design (later tickets): identity/display-name map beyond a
  trivial stub, the propagation pending set, chunking, error_kind mapping.
"""

import asyncio
import contextlib
import datetime
import logging
import threading
import uuid
from typing import Any, Dict, Optional
from typing import Protocol, runtime_checkable

from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.config import Platform

from hermes_reticulum.plugin.identity import (
    IdentityMap,
    is_valid_destination,
    is_parseable_inbound,
)

logger = logging.getLogger("hermes_reticulum.adapter")


@runtime_checkable
class Transport(Protocol):
    """Test seam for the Reticulum/LXMF transport (spec sections 5 and 10).

    The real implementation wraps an LXMF router; tests substitute a fake.
    Callbacks fire from the transport's own thread, never the asyncio loop.
    """

    def send_to(self, destination_hash: str, payload: str) -> bool:
        """Attempt to send ``payload`` to a destination hash. False when no
        interface can carry the packet (maps to a transient failure)."""
        ...

    def register_delivery_callback(self, callback) -> None:
        """Register ``callback(receipt)`` fired on delivery state changes."""
        ...

    def register_failed_callback(self, callback) -> None:
        """Register ``callback(reason)`` fired on delivery failure."""
        ...

    def start(self) -> None:
        """Start the transport (RNS instance / LXMF router)."""
        ...

    def stop(self) -> None:
        """Stop the transport and release its resources."""
        ...


class FakeTransport:
    """In-repo no-op transport used to prove adapter wiring (spec section 10).

    Not a dependency of the real adapter: it is injected for tests. The
    real LXMF transport comes in a later ticket behind this same protocol.
    """

    def __init__(self):
        self.started = False
        self.stopped = False
        self.sent = []
        self._delivery_cb = None
        self._failed_cb = None

    def send_to(self, destination_hash: str, payload: str) -> bool:
        self.sent.append((destination_hash, payload))
        return True

    def register_delivery_callback(self, callback):
        self._delivery_cb = callback

    def register_failed_callback(self, callback):
        self._failed_cb = callback

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class ReticulumPlatformAdapter(BasePlatformAdapter):
    """Gateway platform adapter bridging Reticulum/LXMF into the agent.

    Capability flags follow spec section 2; ``supports_status_text`` and
    ``REQUIRES_EDIT_FINALIZE`` intentionally stay at their inherited
    ``False`` defaults (no typing indicator, no edit support on LXMF).
    """

    # The mesh can start a fresh turn after a previous one ended.
    supports_async_delivery = True
    # LXMF over radio benefits from chunking; the gateway otherwise truncates
    # at MAX_MESSAGE_LENGTH (4096), which is larger than one packet carries.
    splits_long_messages = True
    # Reticulum clients render markdown, including fenced code blocks.
    supports_code_blocks = True

    def __init__(self, config, transport_factory=None, **kwargs):
        # Plugin platforms have no static Platform enum member: Platform._missing_
        # accepts names registered in platform_registry, which the gateway fills
        # in only after this plugin registers itself (registration.py). Until
        # then (tests, standalone imports) we fall back to the local "local"
        # member. The gateway rebinds platform identity from the registry entry
        # at construction time; the pseudo-member lookup here is just so
        # super().__init__ gets a valid member.
        try:
            platform = Platform("reticulum")
        except ValueError:
            platform = Platform.LOCAL
        super().__init__(config=config, platform=platform)
        extra = getattr(config, "extra", {}) or {}

        # Display name (spec section 12) — env is read through the scoped
        # reader, never os.getenv, per spec section 2.
        self.display_name = (
            _get_scoped_secret("RETICULUM_DISPLAY_NAME")
            or extra.get("display_name")
            or "Hermes for Reticulum"
        )
        # Announce interval, minutes; 0 disables periodic re-announce.
        raw_interval = _get_scoped_secret("RETICULUM_ANNOUNCE_INTERVAL")
        if raw_interval is None:
            raw_interval = extra.get("announce_interval", 60)
        self.announce_interval = float(raw_interval)
        self.home_channel = extra.get("home_channel")

        # The transport is a seam: a real LXMF-backed implementation lands in
        # a later ticket. Until then the default is the in-repo fake so the
        # adapter is fully exercisable without a mesh. ``transport_factory``
        # lets tests (and the later real transport) substitute their own.
        self._transport_factory = transport_factory or FakeTransport
        self._transport = None

        # Thread bridge (spec section 5): transport callbacks arrive on the
        # RNS thread and are shuttled to the asyncio loop through a queue
        # drained by a task.
        self._queue = None
        self._drain_task: Optional[asyncio.Task] = None

        # Identity map (spec section 4): chat_id is the destination hash
        # itself; the adapter owns the hash → display-name map.
        # ``_names`` is exposed as a property for the (existing) tests that
        # write to ``adapter._names[...]`` directly.
        self._identity = IdentityMap()

    @property
    def name(self) -> str:
        return "Reticulum"

    @property
    def _names(self) -> Dict[str, str]:
        # Backward-compatible view of the identity map so existing tests
        # (and future code) can read/write ``adapter._names[hash]`` directly.
        return self._identity._names

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Start the transport and the inbound drain loop.

        Idempotent: a second call while up is a no-op returning True.
        ``is_reconnect=True`` tears down and re-initialises from scratch,
        as the gateway's reconnect watcher does after a transport death.
        """
        if self.is_connected and not is_reconnect:
            logger.info("Reticulum: already connected (idempotent connect)")
            return True
        if is_reconnect:
            # Re-initialise from scratch: the watcher calls this after the
            # transport (or process) died, so leftover state is suspect.
            with contextlib.suppress(Exception):
                await self.disconnect()

        transport = self._transport_factory()
        # RNS callbacks arrive from the transport thread; both push events
        # onto the asyncio queue for the drain task (spec section 5).
        self._queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        transport.register_delivery_callback(
            lambda receipt: loop.call_soon_threadsafe(self._queue.put_nowait, receipt)
        )
        transport.register_failed_callback(
            lambda reason: loop.call_soon_threadsafe(
                self._queue.put_nowait, ("failed", reason)
            )
        )
        transport.start()
        self._transport = transport
        self._drain_task = asyncio.create_task(self._drain_loop())
        self._mark_connected()
        logger.info(
            "Reticulum adapter connected (display name %s, announce interval %ss)",
            self.display_name, self.announce_interval * 60,
        )
        return True

    async def disconnect(self) -> None:
        """Stop the drain task and the transport. Safe to call before connect."""
        self._mark_disconnected()
        if self._drain_task is not None:
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._drain_task
            self._drain_task = None
        if self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.stop()
            self._transport = None
        self._queue = None
        logger.info("Reticulum adapter disconnected")

    # ── Inbound thread bridge ────────────────────────────────────────────

    async def _drain_loop(self) -> None:
        """Drain transport events from the queue and hand them to the base
        class ``handle_message``. One event in, one dispatch out."""
        try:
            while True:
                event = await self._queue.get()
                try:
                    await self._dispatch_inbound(event)
                except Exception as e:
                    # Never let one malformed inbound kill the drain loop
                    # (spec section 14).
                    logger.warning("Reticulum: inbound dispatch failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Reticulum: drain loop error: %s", e)

    async def _dispatch_inbound(self, event) -> None:
        """Turn one transport callback event into a gateway MessageEvent.

        Foundation-stage parsing: ``event`` is a ``(source_hash, text)``
        tuple (or a dict with ``source``/``text``) produced by the callback
        registration in ``connect()``. The real transport will deliver
        LXMF receipt objects; this seam keeps the drain shape testable now.
        """
        source_hash, text = self._normalize_inbound(event)
        if source_hash is None:
            # Malformed inbound: drop with a log line and no reply
            # (spec section 14).
            logger.debug("Reticulum: dropping unparseable inbound %r", event)
            return
        # Spec section 14: also drop here if the source is not in the allowlist
        # (the allowlist comes from the platform gate env, see registration.py).
        allowlist = self._allowlist()
        if allowlist and not self._in_allowlist(source_hash, allowlist):
            logger.debug("Reticulum: dropping inbound from unallowed peer %r", source_hash[:8] if isinstance(source_hash, str) else "?")
            return
        source = self.build_source(
            chat_id=source_hash,
            chat_name=self._identity.get_name(source_hash, source_hash),
            chat_type="dm",
            user_id=source_hash,
            user_name=self._identity.get_name(source_hash, source_hash),
        )
        await self.handle_message(
            MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=str(uuid.uuid4()),
                timestamp=datetime.datetime.now(),
            )
        )

    @staticmethod
    def _normalize_inbound(event):
        """Best-effort ``(source_hash, text)`` extraction; (None, None) to drop.

        Spec section 14: also drops when the source hash is not 32 hex chars
        (the gateway's authz layer applies the allowlist separately; this
        predicate here keeps the drain loop safe from unparseable input).
        """
        if isinstance(event, dict):
            source_hash = event.get("source") or event.get("destination")
            text = event.get("text") or event.get("payload")
        elif isinstance(event, (tuple, list)) and len(event) >= 2:
            source_hash, text = event[0], event[1]
        else:
            return None, None
        if not is_valid_destination(source_hash):
            return None, None
        if not isinstance(text, str) or not text:
            return None, None
        return source_hash, text

    # ── Outbound ─────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send text to a destination hash.

        Foundation stage: the transport seam is exercised, but the receipt
        mapping (DELIVERED/FAILED/SENT, spec section 6) waits on the real
        transport, so every failure here returns a well-formed failing
        ``SendResult`` — never a raised exception.
        """
        if not self.is_connected or self._transport is None:
            return SendResult(
                success=False,
                error="Reticulum adapter is not connected",
                retryable=True,
                error_kind="transient",
            )
        if not content or not content.strip():
            # An empty reply is a bug in the turn, not something to send
            # (spec section 14): log it and send nothing.
            logger.warning("Reticulum: refusing to send empty message")
            return SendResult(success=False, error="empty message", error_kind="bad_format")
        if not is_valid_destination(chat_id):
            # A malformed destination is a content error, not a transport one.
            return SendResult(
                success=False,
                error="malformed destination hash",
                retryable=False,
                error_kind="bad_format",
            )
        sent = self._transport.send_to(chat_id, content)
        if not sent:
            # No path / no interface: transient, let the base-class retry
            # logic and the ledger sweep behave (spec section 6).
            return SendResult(
                success=False,
                error="no interface could carry the packet",
                retryable=True,
                error_kind="transient",
            )
        # Foundation-stage seam: the packet reached the transport. The real
        # delivery outcome (DELIVERED vs SENT vs FAILED) is mapped by a
        # follow-on ticket once the LXMF callbacks land here.
        return SendResult(
            success=False,
            error="delivery receipt mapping pending (transport seam)",
            retryable=True,
            error_kind="unknown",
        )

    @staticmethod
    def _is_valid_hash(destination_hash: str) -> bool:
        """A destination is 32 hex characters (LXMF hash). Kept for compat."""
        return is_valid_destination(destination_hash)

    # ── Chat info ────────────────────────────────────────────────────────

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Name and type for a destination hash. Never raises; unknown
        hashes get a fallback built from the hash itself (spec section 10).
        """
        with contextlib.suppress(Exception):
            name = self._identity.get_name(chat_id, str(chat_id))
        return {"name": name, "type": "dm"}

    # ── Access control helpers (spec section 7) ─────────────────────────

    def _allowlist(self) -> Optional[list]:
        """Read the allowlist from the platform gate env (multiplex-safe).

        Returns None when the allowlist is unset/empty (allow-all or no
        allowlist configured — the gateway's authz layer handles the
        allow-all default, see ``authz_mixin.py``). Returns a list of
        raw hex hashes (not normalized) when set.
        """
        try:
            from gateway.platforms._shared import platform_gate_env
            raw = platform_gate_env("HERMES_RETICUM_ALLOWED_USERS", "")
        except ImportError:
            return None
        if not raw.strip():
            return None
        return [h.strip() for h in raw.split(",") if h.strip()]

    @staticmethod
    def _in_allowlist(destination_hash: str, allowlist: list) -> bool:
        """Check ``destination_hash`` against a raw allowlist (colon/space
        tolerant, matching the ACL's ``_normalize_hash`` behaviour)."""
        def norm(h):
            return h.strip().lower().replace(" ", "").replace(":", "")
        target = norm(destination_hash) if isinstance(destination_hash, str) else ""
        return any(norm(h) == target for h in allowlist)

    # ── Typing ───────────────────────────────────────────────────────────

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """LXMF has no typing indicator (spec section 2)."""
