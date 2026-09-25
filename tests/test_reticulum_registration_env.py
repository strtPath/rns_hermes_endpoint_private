"""Env seeding for the Reticulum platform registration.

These pin the contract the README and install scripts document: which vars
seed which ``PlatformConfig.extra`` keys, what happens when the old bridge's
variable names are used instead, and that a missing display name disables the
platform entirely.
"""

import os

import pytest

from hermes_reticulum.plugin import registration


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "RETICULUM_DISPLAY_NAME",
        "RETICULUM_ANNOUNCE_INTERVAL",
        "RETICULUM_STORAGE_PATH",
        "RETICULUM_STORAGE",
        "RETICULUM_RNS_CONFIG_PATH",
        "RETICULUM_HOME_CHANNEL",
        "RETICULUM_HOME_CHANNEL_NAME",
    ):
        monkeypatch.delenv(var, raising=False)


def test_env_table_covers_every_var_the_transport_reads():
    """The seed table must not be shorter than the transport's readers.

    A var absent here still works through the scoped-secret fallback, but it
    would be missing from the documented list, so operators would not know it
    exists. Keep the two in step.
    """
    declared = {row[0] for row in registration._ENV_TABLE}
    for var in (
        "RETICULUM_ANNOUNCE_INTERVAL",
        "RETICULUM_STORAGE_PATH",
        "RETICULUM_RNS_CONFIG_PATH",
    ):
        assert var in declared, f"{var} is read by the transport but not seeded"


def test_no_display_name_disables_the_platform(monkeypatch):
    """No display name => env_enablement returns None => never registers."""
    monkeypatch.setenv("RETICULUM_STORAGE_PATH", "/tmp/whatever")
    assert registration._env_enablement() is None


def test_display_name_yields_extra(monkeypatch):
    monkeypatch.setenv("RETICULUM_DISPLAY_NAME", "test-node")
    extra = registration._env_enablement()
    assert extra["display_name"] == "test-node"


def test_announce_interval_is_seeded_as_float(monkeypatch):
    monkeypatch.setenv("RETICULUM_DISPLAY_NAME", "test-node")
    monkeypatch.setenv("RETICULUM_ANNOUNCE_INTERVAL", "90")
    extra = registration._env_enablement()
    assert extra["announce_interval"] == 90.0
    assert isinstance(extra["announce_interval"], float)


def test_storage_and_rns_config_are_seeded(monkeypatch):
    monkeypatch.setenv("RETICULUM_DISPLAY_NAME", "test-node")
    monkeypatch.setenv("RETICULUM_STORAGE_PATH", "/tmp/storage")
    monkeypatch.setenv("RETICULUM_RNS_CONFIG_PATH", "/tmp/rns")
    extra = registration._env_enablement()
    assert extra["storage_path"] == "/tmp/storage"
    assert extra["rns_config_path"] == "/tmp/rns"


def test_legacy_storage_var_is_accepted(monkeypatch):
    """An operator migrating the bridge .env keeps their storage path.

    The bridge spells it RETICULUM_STORAGE; the plugin reads
    RETICULUM_STORAGE_PATH. Without this fallback the plugin would silently
    use its default and the operator would not know why.
    """
    monkeypatch.setenv("RETICULUM_DISPLAY_NAME", "test-node")
    monkeypatch.setenv("RETICULUM_STORAGE", "/tmp/legacy")
    extra = registration._env_enablement()
    assert extra["storage_path"] == "/tmp/legacy"


def test_new_storage_var_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("RETICULUM_DISPLAY_NAME", "test-node")
    monkeypatch.setenv("RETICULUM_STORAGE_PATH", "/tmp/new")
    monkeypatch.setenv("RETICULUM_STORAGE", "/tmp/legacy")
    extra = registration._env_enablement()
    assert extra["storage_path"] == "/tmp/new"


def test_blank_values_do_not_seed(monkeypatch):
    monkeypatch.setenv("RETICULUM_DISPLAY_NAME", "test-node")
    monkeypatch.setenv("RETICULUM_STORAGE_PATH", "   ")
    extra = registration._env_enablement()
    assert "storage_path" not in extra


def test_home_channel_is_seeded(monkeypatch):
    """Cron delivery target survives the refactor into _seed_extra()."""
    monkeypatch.setenv("RETICULUM_DISPLAY_NAME", "test-node")
    monkeypatch.setenv("RETICULUM_HOME_CHANNEL", "0" * 32)
    extra = registration._env_enablement()
    assert extra["home_channel"]["chat_id"] == "0" * 32


def test_invalid_interval_is_skipped_not_fatal(monkeypatch):
    """A bad float is dropped by the seeder; the transport then validates.

    seed_extra_from_env suppresses ValueError, so a typo here must not stop
    the platform registering - the transport's own validation is the gate.
    """
    monkeypatch.setenv("RETICULUM_DISPLAY_NAME", "test-node")
    monkeypatch.setenv("RETICULUM_ANNOUNCE_INTERVAL", "not-a-number")
    extra = registration._env_enablement()
    assert extra is not None
    assert "announce_interval" not in extra


# ── The gateway's enable gate (is_connected) ──────────────────────────────
# gateway/config_env.py::_enable_plugin_platform skips a platform whose
# is_connected returns False, at DEBUG level. A wrong answer here is silent:
# the plugin registers, then no adapter is ever built.


def test_is_connected_is_true_when_unconfigured():
    """The gate asks "is this platform set up", not "is a socket open".

    An earlier version read RETICULUM_CONNECTED, which nothing sets, so the
    answer was permanently False and the adapter was never constructed.
    """
    assert registration._is_connected(None) is True


def test_is_connected_ignores_a_runtime_state_var(monkeypatch):
    """The old runtime flag must not change the verdict either way."""
    monkeypatch.setenv("RETICULUM_CONNECTED", "false")
    assert registration._is_connected(None) is True
    monkeypatch.setenv("RETICULUM_CONNECTED", "true")
    assert registration._is_connected(None) is True
