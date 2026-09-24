"""
Structural and lifecycle tests for the Reticulum platform adapter
(spec section 10 test seams).

The real LXMF transport does not exist yet (later ticket); these tests
run the adapter against the in-repo ``FakeTransport`` behind the
``Transport`` protocol, exactly the seam the spec calls for.

No test here asserts on platform lists or command counts (spec section 10).
"""

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from gateway.platforms.base import BasePlatformAdapter, SendResult

from hermes_reticulum.plugin.adapter import (
    FakeTransport,
    ReticulumPlatformAdapter,
    Transport,
)


class PlatformConfig:
    """Minimal stand-in for the gateway's PlatformConfig dataclass."""

    def __init__(self, extra=None):
        self.extra = extra or {}
        self.enabled = True
        self.token = None
        self.api_key = None
        self.home_channel = None


class CountingFactory:
    """Transport factory that hands out a single shared fake transport."""

    def __init__(self):
        self.transport = FakeTransport()
        self.constructions = 0

    def __call__(self):
        self.constructions += 1
        return self.transport


class HandlerRecorder:
    """Captures the events the base-class handle_message forwards."""

    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


def make_adapter(recorder=None):
    adapter = ReticulumPlatformAdapter(
        PlatformConfig(), transport_factory=CountingFactory()
    )
    if recorder is not None:
        adapter.set_message_handler(recorder)
    return adapter


# ── Structure ──────────────────────────────────────────────────────────────


def test_instantiation_satisfies_abstract_interface():
    """All four abstract methods are implemented: instantiation succeeds."""
    adapter = ReticulumPlatformAdapter(PlatformConfig())
    assert isinstance(adapter, BasePlatformAdapter)


def test_transport_protocol_is_structural():
    assert isinstance(FakeTransport(), Transport)


# ── Lifecycle (spec section 10) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_twice_is_idempotent():
    factory = CountingFactory()
    adapter = ReticulumPlatformAdapter(PlatformConfig(), transport_factory=factory)
    assert await adapter.connect() is True
    assert await adapter.connect() is True
    assert factory.constructions == 1
    assert adapter.is_connected
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_disconnect_then_connect_works():
    adapter = make_adapter()
    assert await adapter.connect() is True
    await adapter.disconnect()
    assert not adapter.is_connected
    assert await adapter.connect() is True
    assert adapter.is_connected
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_disconnect_without_connect_does_not_raise():
    adapter = make_adapter()
    await adapter.disconnect()  # must not raise
    assert not adapter.is_connected


@pytest.mark.asyncio
async def test_is_reconnect_reinitialises_from_scratch():
    factory = CountingFactory()
    adapter = ReticulumPlatformAdapter(PlatformConfig(), transport_factory=factory)
    assert await adapter.connect() is True
    assert await adapter.connect(is_reconnect=True) is True
    assert factory.constructions == 2
    assert adapter.is_connected
    await adapter.disconnect()


# ── Inbound thread bridge (spec section 10) ────────────────────────────────


@pytest.mark.asyncio
async def test_foreign_callback_reaches_handle_message_exactly_once():
    """An event pushed from a foreign thread (the way RNS would) is
    delivered to handle_message exactly once, then the drain task ends
    cleanly on disconnect."""
    recorder = HandlerRecorder()
    factory = CountingFactory()
    adapter = ReticulumPlatformAdapter(
        PlatformConfig(), transport_factory=factory,
    )
    adapter.set_message_handler(recorder)
    assert await adapter.connect() is True

    # connect() installed the delivery callback on the fake transport and
    # stashed it in FakeTransport._delivery_cb; fire it from a foreign
    # thread to simulate the RNS callback thread.
    def fire():
        cb = factory.transport._delivery_cb
        if cb:
            cb(("0" * 32, "hello mesh"))

    t = threading.Thread(target=fire)
    t.start()
    t.join()

    deadline = time.monotonic() + 5.0
    while not recorder.events and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    await adapter.disconnect()
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.text == "hello mesh"
    assert event.source.chat_id == "0" * 32


@pytest.mark.asyncio
async def test_drain_task_ends_cleanly_on_disconnect():
    adapter = make_adapter()
    assert await adapter.connect() is True
    drain_task = adapter._drain_task
    assert drain_task is not None
    assert not drain_task.done()
    await adapter.disconnect()
    assert drain_task.cancelled()
    # Nothing left over on the adapter.
    assert adapter._drain_task is None
    assert adapter._queue is None


# ── get_chat_info (spec section 10) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chat_info_known_hash_uses_name_map():
    adapter = make_adapter()
    adapter._names["0" * 32] = "trusted peer"
    info = await adapter.get_chat_info("0" * 32)
    assert info == {"name": "trusted peer", "type": "dm"}


@pytest.mark.asyncio
async def test_get_chat_info_unknown_hash_falls_back():
    adapter = make_adapter()
    info = await adapter.get_chat_info("0" * 32)
    assert info["type"] == "dm"
    assert info["name"] == "0" * 32


@pytest.mark.asyncio
async def test_get_chat_info_never_raises():
    adapter = make_adapter()
    for weird in ("", "not-a-hash", "zz" * 16, "0" * 32):
        info = await adapter.get_chat_info(weird)
        assert "name" in info and "type" in info


# ── send (foundation-stage seam) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_not_connected_fails_well_formed():
    adapter = make_adapter()
    result = await adapter.send("0" * 32, "hi")
    assert isinstance(result, SendResult)
    assert result.success is False
    assert result.retryable is True
    assert result.error_kind == "transient"


@pytest.mark.asyncio
async def test_send_malformed_hash_fails_bad_format():
    adapter = make_adapter()
    await adapter.connect()
    result = await adapter.send("not-a-real-hash", "hi")
    assert result.success is False
    assert result.retryable is False
    assert result.error_kind == "bad_format"
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_send_empty_content_rejected():
    adapter = make_adapter()
    await adapter.connect()
    result = await adapter.send("0" * 32, "   ")
    assert result.success is False
    assert result.error_kind == "bad_format"
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_send_through_fake_transport_returns_failing_seam_result():
    """send() exercises the seam and returns a well-formed SendResult —
    never raises. The real receipt mapping lands with the LXMF transport."""
    adapter = make_adapter()
    await adapter.connect()
    result = await adapter.send("0" * 32, "hello")
    assert isinstance(result, SendResult)
    assert result.success is False
    assert result.error is not None
    # The fake transport saw the packet.
    assert adapter._transport.sent == [("0" * 32, "hello")]
    await adapter.disconnect()
