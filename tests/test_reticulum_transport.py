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
        # Protocol is structural: check the required callables exist. Inbound
        # and outbound are separate slots by design (the collision between them
        # echoed every reply back in as inbound traffic).
        for method in (
            "send_to",
            "register_inbound_callback",
            "register_outbound_callback",
            "register_failed_callback",
            "start",
            "stop",
        ):
            assert callable(getattr(type(t), method))


# ── send_to with a stubbed RNS ────────────────────────────────────────


class _FakeLXMessage:
    # Values mirror the real LXMessage constants (LXMessage.py:14-31). A fake
    # with different numbers silently passes against the wrong comparisons.
    GENERATING = 0x00
    OUTBOUND = 0x01
    SENDING = 0x02
    SENT = 0x04
    DELIVERED = 0x08
    REJECTED = 0xFD
    CANCELLED = 0xFE
    FAILED = 0xFF

    OPPORTUNISTIC = 0x01
    DIRECT = 0x02
    PROPAGATED = 0x03

    @staticmethod
    def state_name(state):
        return {
            0x00: "generating",
            0x01: "outbound",
            0x02: "sending",
            0x04: "sent",
            0x08: "delivered",
            0xFD: "rejected",
            0xFE: "cancelled",
            0xFF: "failed",
        }.get(state, "unknown")

    def __init__(self, dest, source, content="", **kwargs):
        self.dest = dest
        self.source = source
        self.content = content
        # Real LXMessage turns its constructor kwargs into attributes
        # (include_ticket, desired_method, ...); the fake must too, or a
        # test reading them passes vacuously against the wrong call shape.
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.kwargs = kwargs
        self.delivery_callback = None
        self.failed_callback = None
        # Real messages carry a live state; OUTBOUND is the state a message is
        # in immediately after handle_outbound queues it.
        self.state = _FakeLXMessage.OUTBOUND

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
        # Mirror the real router's propagation surface. Without these the
        # policy call would AttributeError and the tests would prove nothing
        # about the branch they mean to cover.
        self.autopeer = True
        self.autopeer_maxdepth = 4
        self.outbound_propagation_node = None

    def register_delivery_identity(self, identity, display_name=None, stamp_cost=None):
        return _FakeDestination()

    def register_delivery_callback(self, cb):
        self.delivery_cb = cb

    def handle_outbound(self, lxm):
        self.outbound.append(lxm)

    def set_outbound_propagation_node(self, destination_hash):
        self.outbound_propagation_node = destination_hash

    def get_outbound_propagation_node(self):
        return self.outbound_propagation_node


class _FakeTransport:
    """Stands in for RNS.Transport.

    The path methods are per-instance Mocks, not staticmethods, so a test can
    assert whether a path was requested and control what has_path answers. A
    staticmethod here would make ``assert_not_called`` meaningless.
    """

    def __init__(self):
        self.started_with = None
        self.exit_calls = 0
        self.has_path = mock.Mock(return_value=False)
        self.hops_to = mock.Mock(return_value=128)
        self.request_path = mock.Mock(return_value=None)

    @staticmethod
    def start(reticulum_instance):
        FakeTransportHolder.started_with = reticulum_instance

    @staticmethod
    def exit_handler():
        FakeTransportHolder.exit_calls += 1


class FakeTransportHolder:
    started_with = None
    exit_calls = 0


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch):
    """Zero the path wait and the outcome grace period for every test.

    Both are genuine behaviour, asserted separately, but sleeping through
    them costs ~60s of suite time and a mocked sleep would spin the loop for
    the full real deadline (time.time does not advance under a mock).
    """
    monkeypatch.setattr(transport_module.ReticulumTransport, "PATH_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(transport_module.ReticulumTransport, "OUTCOME_GRACE_SECONDS", 0.0)


@pytest.fixture
def fake_rns(monkeypatch):
    """Stub RNS/LXMF modules so no real stack is touched."""
    identity = mock.Mock()
    identity.recall = mock.Mock(return_value=None)

    fake_rns_mod = types.ModuleType("RNS")
    fake_rns_mod.__version__ = "1.5.4"

    def _make_ret():
        return mock.Mock(
            is_shared_instance=True,
            is_connected_to_shared_instance=False,
            is_standalone_instance=False,
            internal_identity=mock.Mock(return_value=None),
        )
    fake_rns_mod.Reticulum = mock.Mock(side_effect=_make_ret)
    fake_rns_mod.Destination = _FakeDestination
    # An INSTANCE, so the path methods are the per-test Mocks the assertions
    # target. Assigning the class would expose unbound mocks and make
    # assert_not_called on request_path meaningless.
    fake_rns_mod.Transport = _FakeTransport()
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

        inbound = []
        outbound = []
        failed = []
        t.register_inbound_callback(
            lambda source, payload: inbound.append((source, payload))
        )
        t.register_outbound_callback(
            lambda payload, state: outbound.append((payload, state))
        )
        t.register_failed_callback(lambda reason: failed.append(reason))

        assert t.send_to(FAKE_HASH, "hello mesh") is True
        router = t.router
        assert len(router.outbound) == 1
        msg = router.outbound[0]
        assert msg.content == "hello mesh"
        assert msg.include_ticket is True
        assert callable(msg.delivery_callback)
        assert callable(msg.failed_callback)

        # The per-message callback reports OUR OWN message's fate, so it must
        # land in the outbound slot. Routing it to the inbound handler is the
        # defect that echoed every reply back in as peer traffic.
        msg.state = _FakeLXMessage.DELIVERED
        msg.delivery_callback(msg)
        assert outbound == [("hello mesh", _FakeLXMessage.DELIVERED)]
        assert inbound == [], (
            "an outgoing message must never reach the inbound slot; that is "
            "the echo defect (2026-09-25 findings)"
        )

        # A genuine inbound message arrives on the ROUTER callback instead.
        class _FakeInbound:
            source_hash = bytes.fromhex(FAKE_HASH2)

            def content_as_string(self):
                return "ping"

        t._on_router_delivery(_FakeInbound())
        assert inbound == [(FAKE_HASH2, "ping")]
        assert len(outbound) == 1, "inbound must not add an outbound outcome"

        msg.failed_callback(mock.Mock(state=3))
        assert len(failed) == 1
        assert isinstance(failed[0], str)

    def test_send_to_malformed_hash_returns_false(self, fake_rns, tmp_path):
        t = self._started_transport(fake_rns, tmp_path)
        assert t.send_to("xyz", "nope") is False
        assert t.send_to("", "nope") is False

    def test_send_to_exception_returns_false(self, fake_rns, tmp_path):
        """A raising dispatch is reported as a failed send, not an exception.

        handle_outbound is the real entry point (LXMF has no
        message_for_destination), so that is the seam to break.
        """
        fake_rns.Identity.recall.return_value = mock.Mock()
        t = self._started_transport(fake_rns, tmp_path)
        t.router.handle_outbound = mock.Mock(side_effect=RuntimeError("boom"))
        assert t.send_to(FAKE_HASH, "hello") is False


# ── Path-aware method selection ───────────────────────────────────────────
# A reply to an offline phone has no path. DIRECT cancels in about a second
# (MAX_PATHLESS_TRIES = 1), so a reply must go via a propagation node when one
# is configured, and must be reported as FAILED when it does not leave. Before
# this, send_to returned True for a message the router had cancelled, so every
# dropped reply looked delivered.


class TestPathAwareSend:
    def _started_transport(self, fake_rns, tmp_path, extra=None):
        t = transport_module.ReticulumTransport(
            storage_path=str(tmp_path / "storage"), extra=extra or {}
        )
        t.start()
        return t

    def test_warm_path_sends_direct(self, fake_rns, tmp_path):
        fake_rns.Identity.recall.return_value = mock.Mock()
        fake_rns.Transport.has_path.return_value = True
        fake_rns.Transport.hops_to.return_value = 2
        t = self._started_transport(fake_rns, tmp_path)
        assert t.send_to(FAKE_HASH, "hello") is True
        assert t.router.outbound[0].desired_method == _FakeLXMessage.DIRECT
        # A warm path must not pay the path-request wait.
        fake_rns.Transport.request_path.assert_not_called()

    def test_cold_path_is_requested_before_giving_up(self, fake_rns, tmp_path):
        """DIRECT does not request paths; we must, or it cancels instantly."""
        fake_rns.Identity.recall.return_value = mock.Mock()
        fake_rns.Transport.has_path.return_value = False
        t = self._started_transport(fake_rns, tmp_path)
        t.PATH_WAIT_SECONDS = 0.0
        t.send_to(FAKE_HASH, "hello")
        fake_rns.Transport.request_path.assert_called()

    def test_no_path_with_node_sends_propagated(self, fake_rns, tmp_path):
        fake_rns.Identity.recall.return_value = mock.Mock()
        fake_rns.Transport.has_path.return_value = False
        t = self._started_transport(fake_rns, tmp_path)
        t.router.outbound_propagation_node = bytes.fromhex("ab" * 16)
        t.PATH_WAIT_SECONDS = 0.0
        assert t.send_to(FAKE_HASH, "hello") is True
        assert t.router.outbound[0].desired_method == _FakeLXMessage.PROPAGATED

    def test_no_path_and_no_node_sends_direct_and_warns(self, fake_rns, tmp_path, caplog):
        fake_rns.Identity.recall.return_value = mock.Mock()
        fake_rns.Transport.has_path.return_value = False
        t = self._started_transport(fake_rns, tmp_path)
        t.router.outbound_propagation_node = None
        t.PATH_WAIT_SECONDS = 0.0
        t.send_to(FAKE_HASH, "hello")
        assert t.router.outbound[0].desired_method == _FakeLXMessage.DIRECT

    def test_propagation_off_never_propagates(self, fake_rns, tmp_path):
        fake_rns.Identity.recall.return_value = mock.Mock()
        fake_rns.Transport.has_path.return_value = False
        t = self._started_transport(
            fake_rns, tmp_path, extra={"propagation_node": "off"}
        )
        t.router.outbound_propagation_node = bytes.fromhex("ab" * 16)
        t.PATH_WAIT_SECONDS = 0.0
        t.send_to(FAKE_HASH, "hello")
        assert t.router.outbound[0].desired_method == _FakeLXMessage.DIRECT

    def test_cancelled_message_reports_false(self, fake_rns, tmp_path):
        """The whole point: a cancelled reply must not look delivered."""
        fake_rns.Identity.recall.return_value = mock.Mock()
        fake_rns.Transport.has_path.return_value = True
        fake_rns.Transport.hops_to.return_value = 2
        t = self._started_transport(fake_rns, tmp_path)

        def cancel(lxm):
            t.router.outbound.append(lxm)
            lxm.state = _FakeLXMessage.CANCELLED

        t.router.handle_outbound = mock.Mock(side_effect=cancel)
        assert t.send_to(FAKE_HASH, "hello") is False

    def test_failed_message_reports_false(self, fake_rns, tmp_path):
        fake_rns.Identity.recall.return_value = mock.Mock()
        fake_rns.Transport.has_path.return_value = True
        t = self._started_transport(fake_rns, tmp_path)

        def fail(lxm):
            t.router.outbound.append(lxm)
            lxm.state = _FakeLXMessage.FAILED

        t.router.handle_outbound = mock.Mock(side_effect=fail)
        assert t.send_to(FAKE_HASH, "hello") is False

    def test_propagated_sent_state_counts_as_success(self, fake_rns, tmp_path):
        """SENT is terminal success: a propagation node accepted the message."""
        fake_rns.Identity.recall.return_value = mock.Mock()
        fake_rns.Transport.has_path.return_value = True
        t = self._started_transport(fake_rns, tmp_path)

        def sent(lxm):
            t.router.outbound.append(lxm)
            lxm.state = _FakeLXMessage.SENT

        t.router.handle_outbound = mock.Mock(side_effect=sent)
        assert t.send_to(FAKE_HASH, "hello") is True

    def test_no_identity_and_no_path_returns_false(self, fake_rns, tmp_path):
        fake_rns.Identity.recall.return_value = None
        fake_rns.Transport.has_path.return_value = False
        t = self._started_transport(fake_rns, tmp_path)
        t.PATH_WAIT_SECONDS = 0.0
        assert t.send_to(FAKE_HASH, "hello") is False
        assert t.router.outbound == []


# ── Propagation policy ────────────────────────────────────────────────────


class TestPropagationPolicy:
    def _started_transport(self, fake_rns, tmp_path, extra=None):
        t = transport_module.ReticulumTransport(
            storage_path=str(tmp_path / "storage"), extra=extra or {}
        )
        t.start()
        return t

    def test_pinned_node_is_set_on_the_router(self, fake_rns, tmp_path):
        node = "0badc0de0badc0de0badc0de0badc0de"
        t = self._started_transport(fake_rns, tmp_path, extra={"propagation_node": node})
        assert t.router.outbound_propagation_node == bytes.fromhex(node)
        assert t.propagation_mode == "pinned"

    def test_off_disables_autopeer(self, fake_rns, tmp_path):
        t = self._started_transport(fake_rns, tmp_path, extra={"propagation_node": "off"})
        assert t.router.autopeer is False
        assert t.router.outbound_propagation_node is None

    def test_auto_leaves_autopeer_alone(self, fake_rns, tmp_path):
        t = self._started_transport(fake_rns, tmp_path, extra={"propagation_node": "auto"})
        assert t.router.autopeer is True
        assert t.router.outbound_propagation_node is None

    def test_unset_defaults_to_auto(self, fake_rns, tmp_path, monkeypatch):
        # Isolate the env: a real machine running the bridge has this set in
        # .env, and a config reader that falls through to the live environment
        # would flip this test's answer without any code change.
        monkeypatch.delenv("RETICULUM_PROPAGATION_NODE", raising=False)
        t = self._started_transport(fake_rns, tmp_path)
        assert t.propagation_mode == "auto"

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-hash",
            "0badc0de0badc0de0badc0de0badc0d",  # one char short
            "gggggggggggggggggggggggggggggggg",  # right length, not hex
            "yes",
        ],
    )
    def test_invalid_value_fails_loudly(self, bad):
        """A typo must not silently change delivery policy."""
        with pytest.raises(ValueError):
            transport_module.validate_propagation_node(bad)

    def test_hash_is_case_insensitive(self):
        mode, h = transport_module.validate_propagation_node("0BADC0DE0BADC0DE0BADC0DE0BADC0DE")
        assert mode == "pinned"
        assert h == "0badc0de0badc0de0badc0de0badc0de"


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
    def _stub_env(self, monkeypatch):
        # Same isolation the construction tests use: read through the real
        # reader but pin the env empty, so an operator who has configured
        # RETICULUM_DISPLAY_NAME on their machine does not change the verdict.
        def fake_get(name, default=None, **kwargs):
            val = os.environ.get(name)
            return default if val is None else val
        monkeypatch.setattr(transport_module, "_get_scoped_secret", fake_get)
        for var in ("RETICULUM_DISPLAY_NAME",):
            monkeypatch.delenv(var, raising=False)

    def test_set_display_name_updates_callable_app_data(self, fake_rns, tmp_path, monkeypatch):
        self._stub_env(monkeypatch)
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
