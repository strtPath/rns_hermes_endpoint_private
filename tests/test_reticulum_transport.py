"""
Tests for the real LXMF-backed transport (plugin/transport.py).

No radio is required: RNS/LXMF are stubbed so the suite passes on a
machine without a mesh. The interval-validation tests exercise the real
module-level validator (no stubs involved) — that is the highest-value
part of the ticket. What is NOT covered here: real packet delivery,
real announces, real shared-instance behaviour — all need a live mesh.
"""

import math
import os
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hermes_reticulum.plugin import transport as transport_module

# Obvious placeholder hashes only (PII rule for the public repo).
FAKE_HASH = "0" * 32
FAKE_HASH2 = "1" * 32


# ── Interval validation (real code, no stubs) ─────────────────────────


class TestValidateAnnounceInterval:
    def test_zero_is_legal(self):
        assert transport_module.validate_announce_interval(0) == 0.0
        assert transport_module.validate_announce_interval(0.0) == 0.0

    def test_floor_is_accepted(self):
        assert transport_module.validate_announce_interval(1.0) == 1.0

    def test_default_is_60_minutes(self):
        assert transport_module.DEFAULT_ANNOUNCE_INTERVAL_MIN == 60.0

    def test_typical_values_accepted(self):
        for v in (1, 20, 30, 60, 120.5):
            assert transport_module.validate_announce_interval(v) == float(v)

    def test_below_floor_rejected(self):
        for v in (0.1, 0.5, 0.99):
            with pytest.raises(ValueError):
                transport_module.validate_announce_interval(v)

    def test_negative_rejected(self):
        for v in (-1, -0.5, -60):
            with pytest.raises(ValueError):
                transport_module.validate_announce_interval(v)

    def test_nan_rejected(self):
        with pytest.raises(ValueError):
            transport_module.validate_announce_interval(math.nan)

    def test_inf_rejected(self):
        with pytest.raises(ValueError):
            transport_module.validate_announce_interval(math.inf)

    def test_negative_inf_rejected(self):
        with pytest.raises(ValueError):
            transport_module.validate_announce_interval(-math.inf)

    def test_non_numeric_rejected(self):
        for v in ("abc", None, [60]):
            with pytest.raises(ValueError):
                transport_module.validate_announce_interval(v)

    def test_string_number_accepted(self):
        # env values arrive as strings through the scoped reader
        assert transport_module.validate_announce_interval("45") == 45.0


# ── Construction ──────────────────────────────────────────────────────


class TestConstruction:
    def _stub_env(self, monkeypatch):
        # Default-profile behaviour: scoped reader falls back to os.environ.
        def fake_get(name, default=None, **kwargs):
            val = os.environ.get(name)
            return default if val is None else val
        monkeypatch.setattr(transport_module, "_get_scoped_secret", fake_get)

    def test_default_interval_is_60(self, monkeypatch):
        self._stub_env(monkeypatch)
        for var in ("RETICULUM_ANNOUNCE_INTERVAL", "RETICULUM_DISPLAY_NAME",
                    "RETICULUM_STORAGE_PATH", "RETICULUM_RNS_CONFIG_PATH"):
            monkeypatch.delenv(var, raising=False)
        t = transport_module.ReticulumTransport()
        assert t.announce_interval_min == 60.0
        assert t.display_name == "Hermes for Reticulum"

    def test_extra_overrides_env(self, monkeypatch):
        self._stub_env(monkeypatch)
        monkeypatch.setenv("RETICULUM_ANNOUNCE_INTERVAL", "90")
        t = transport_module.ReticulumTransport(extra={"announce_interval": 20})
        assert t.announce_interval_min == 20.0

    def test_env_fallback_used(self, monkeypatch):
        self._stub_env(monkeypatch)
        monkeypatch.setenv("RETICULUM_ANNOUNCE_INTERVAL", "90")
        t = transport_module.ReticulumTransport()
        assert t.announce_interval_min == 90.0

    def test_invalid_interval_fails_loudly_at_startup(self, monkeypatch):
        self._stub_env(monkeypatch)
        for v in (math.nan, math.inf, -math.inf, -1, 0.5, "garbage"):
            with pytest.raises(ValueError):
                transport_module.ReticulumTransport(extra={"announce_interval": v})

    def test_zero_interval_legal_via_extra(self, monkeypatch):
        self._stub_env(monkeypatch)
        t = transport_module.ReticulumTransport(extra={"announce_interval": 0})
        assert t.announce_interval_min == 0.0

    def test_implements_protocol_structurally(self):
        t = transport_module.ReticulumTransport.__new__(
            transport_module.ReticulumTransport
        )
        # Protocol is structural: check the required callables exist.
        for method in ("send_to", "register_delivery_callback",
                       "register_failed_callback", "start", "stop"):
            assert callable(getattr(type(t), method))


# ── send_to with a stubbed RNS ────────────────────────────────────────


class _FakeLXMessage:
    DIRECT = "DIRECT"
    OPPORTUNISTIC = "OPPORTUNISTIC"
    PROPAGATED = "PROPAGATED"
    SENT = 4
    FAILED = 6

    @staticmethod
    def state_name(state):
        return {4: "sent", 6: "failed"}.get(state, "unknown")

    def __init__(self, dest, source, content="", **kwargs):
        self.dest = dest
        self.source = source
        self.content = content
        self.kwargs = kwargs
        self.delivery_callback = None
        self.failed_callback = None

    def register_delivery_callback(self, cb):
        self.delivery_callback = cb

    def register_failed_callback(self, cb):
        self.failed_callback = cb

    def send(self):
        return True


class _FakeDestination:
    OUT = "OUT"
    IN = "IN"
    SINGLE = "SINGLE"

    def __init__(self, *args):
        self.args = args

    def set_default_app_data(self, app_data):
        self.default_app_data = app_data

    def announce(self, app_data=None, **kwargs):
        self.announced = (app_data, kwargs)


class _FakeRouter:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.outbound = []
        self.delivery_cb = None

    def register_delivery_identity(self, identity, display_name=None, stamp_cost=None):
        return _FakeDestination()

    def register_delivery_callback(self, cb):
        self.delivery_cb = cb

    def handle_outbound(self, lxm):
        self.outbound.append(lxm)

    def message_for_destination(self, destination, content, **kwargs):
        msg = _FakeLXMessage(destination, None, content, **kwargs)
        self.outbound.append(msg)
        return msg


class _FakeTransport:
    def __init__(self):
        self.started_with = None
        self.exit_calls = 0

    @staticmethod
    def start(reticulum_instance):
        FakeTransportHolder.started_with = reticulum_instance

    @staticmethod
    def exit_handler():
        FakeTransportHolder.exit_calls += 1

    @staticmethod
    def request_path(hash, **kwargs):
        pass


class FakeTransportHolder:
    started_with = None
    exit_calls = 0


@pytest.fixture
def fake_rns(monkeypatch):
    """Stub RNS/LXMF modules so no real stack is touched."""
    identity = mock.Mock()
    identity.recall = mock.Mock(return_value=None)

    fake_rns_mod = types.ModuleType("RNS")
    fake_rns_mod.__version__ = "1.5.4"
    _make_ret = lambda: mock.Mock(
        is_shared_instance=True,
        is_connected_to_shared_instance=False,
        is_standalone_instance=False,
        internal_identity=mock.Mock(return_value=None),
    )
    fake_rns_mod.Reticulum = mock.Mock(side_effect=_make_ret)
    fake_rns_mod.Destination = _FakeDestination
    fake_rns_mod.Transport = _FakeTransport
    fake_rns_mod.Identity = mock.Mock()
    fake_rns_mod.Identity.recall = mock.Mock(return_value=None)

    fake_lxmf_mod = types.ModuleType("LXMF")
    fake_lxmf_mod.APP_NAME = "lxmf"
    fake_lxmf_mod.LXMessage = _FakeLXMessage
    fake_lxmf_mod.LXMRouter = _FakeRouter

    monkeypatch.setattr(transport_module, "RNS", fake_rns_mod)
    monkeypatch.setattr(transport_module, "LXMF", fake_lxmf_mod)

    holder = FakeTransportHolder
    holder.started_with = None
    holder.exit_calls = 0

    yield fake_rns_mod

    # identity re-calls for the send path tests:
    fake_rns_mod.Identity.recall = mock.Mock(return_value=None)


class TestSendTo:
    def _started_transport(self, fake_rns, tmp_path):
        holder = FakeTransportHolder
        t = transport_module.ReticulumTransport(
            storage_path=str(tmp_path / "storage")
        )
        t.start()
        return t

    def test_send_to_returns_false_when_not_started(self, fake_rns):
        t = transport_module.ReticulumTransport()
        assert t.send_to(FAKE_HASH, "hello") is False

    def test_send_to_unknown_identity_returns_false(self, fake_rns, tmp_path):
        fake_rns.Identity.recall.return_value = None
        t = self._started_transport(fake_rns, tmp_path)
        # Avoid the 8x1s identity-recall poll in the test.
        with mock.patch.object(transport_module, "time") as fake_time:
            fake_time.sleep = mock.Mock()
            assert t.send_to(FAKE_HASH, "hello") is False
        # No message was handed to the router.
        assert t.router.outbound == []

    def test_send_to_builds_message_and_hands_to_router(self, fake_rns, tmp_path):
        fake_identity = mock.Mock()
        fake_rns.Identity.recall.return_value = fake_identity
        t = self._started_transport(fake_rns, tmp_path)

        received = []
        failed = []
        t.register_delivery_callback(lambda receipt: received.append(receipt))
        t.register_failed_callback(lambda reason: failed.append(reason))

        assert t.send_to(FAKE_HASH, "hello mesh") is True
        router = t.router
        assert len(router.outbound) == 1
        msg = router.outbound[0]
        assert msg.content == "hello mesh"
        assert msg.include_ticket is True
        assert callable(msg.delivery_callback)
        assert callable(msg.failed_callback)

        # Simulate the RNS thread firing the per-message callbacks.
        class _FakeInbound:
            source_hash = bytes.fromhex(FAKE_HASH2)
            def content_as_string(self):
                return "ping"
        msg.delivery_callback(_FakeInbound())
        assert received == [(FAKE_HASH2, "ping")]

        msg.failed_callback(mock.Mock(state=3))
        assert len(failed) == 1
        assert isinstance(failed[0], str)

    def test_send_to_malformed_hash_returns_false(self, fake_rns, tmp_path):
        t = self._started_transport(fake_rns, tmp_path)
        assert t.send_to("xyz", "nope") is False
        assert t.send_to("", "nope") is False

    def test_send_to_exception_returns_false(self, fake_rns, tmp_path):
        fake_rns.Identity.recall.return_value = mock.Mock()
        t = self._started_transport(fake_rns, tmp_path)
        msg_holder = {}
        real_msg_for = t.router.message_for_destination
        def _boom(destination, content, **kwargs):
            m = real_msg_for(destination, content, **kwargs)
            m.send = mock.Mock(side_effect=RuntimeError("boom"))
            msg_holder["m"] = m
            return m
        t.router.message_for_destination = _boom
        assert t.send_to(FAKE_HASH, "hello") is False


# ── start/stop lifecycle with stubs ───────────────────────────────────


class TestLifecycle:
    def _make(self, tmp_path):
        return transport_module.ReticulumTransport(
            storage_path=str(tmp_path / "storage")
        )

    def test_start_is_idempotent(self, fake_rns, tmp_path):
        holder = FakeTransportHolder
        t = self._make(tmp_path)
        t.start()
        assert holder.started_with is not None
        first = holder.started_with
        t.start()  # second start: no-op
        assert holder.started_with is first
        assert t.is_running is True

    def test_start_after_stop_reinitialises(self, fake_rns, tmp_path):
        holder = FakeTransportHolder
        t = self._make(tmp_path)
        t.start()
        first = holder.started_with
        t.stop()
        assert t.is_running is False
        assert holder.exit_calls == 1
        t.start()
        assert t.is_running is True
        assert holder.started_with is not first  # fresh Reticulum instance

    def test_stop_without_start_is_safe(self, fake_rns, tmp_path):
        holder = FakeTransportHolder
        t = self._make(tmp_path)
        t.stop()
        assert holder.exit_calls == 0
        assert t.is_running is False

    def test_double_stop_is_safe(self, fake_rns, tmp_path):
        holder = FakeTransportHolder
        t = self._make(tmp_path)
        t.start()
        t.stop()
        t.stop()
        assert holder.exit_calls == 1  # exit_handler fired exactly once


# ── Display name runtime change ───────────────────────────────────────


class TestDisplayName:
    def test_set_display_name_updates_callable_app_data(self, fake_rns, tmp_path):
        t = self._make_tested(fake_rns, tmp_path)
        assert t.display_name == "Hermes for Reticulum"
        t.set_display_name("Mesh Hermes")
        assert t.display_name == "Mesh Hermes"
        # The destination's default app data is a callable returning bytes.
        app_data = t.destination.default_app_data
        assert callable(app_data)
        assert app_data() == b"Mesh Hermes"

    def test_set_display_name_before_start_only_updates_state(self, tmp_path):
        t = transport_module.ReticulumTransport(
            storage_path=str(tmp_path / "storage")
        )
        t.set_display_name("Early Name")
        assert t.display_name == "Early Name"

    def _make_tested(self, fake_rns, tmp_path):
        t = transport_module.ReticulumTransport(
            storage_path=str(tmp_path / "storage"),
            display_name="Hermes for Reticulum",
        )
        t.start()
        return t
