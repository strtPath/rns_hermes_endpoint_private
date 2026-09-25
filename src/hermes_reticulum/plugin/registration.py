"""
Plugin Registration — registers Reticulum/LXMF with the Hermes gateway.

This module is the entry point called by the Hermes plugin system.
It registers a platform so the gateway discovers and instantiates
the Reticulum adapter automatically.

``check_reticulum_requirements`` remains exported because the installed
``~/.hermes/plugins/reticulum`` shim's ``register(ctx)`` wraps it (it is
the ``check_fn`` that gates platform enablement); do not delete it without
updating that shim.
"""

import logging

from gateway.platforms._shared import (
    get_scoped_secret as _get_scoped_secret,
    seed_extra_from_env as _seed_extra_from_env,
)

logger = logging.getLogger("hermes_reticulum.registration")

# (ENV_VAR, extra_key, converter) table consumed by seed_extra_from_env.
# This must list every var the transport reads, or the README and the install
# scripts document a shorter list than the code honours.
#
# display_name is deliberately NOT in the table: env_enablement requires it
# and returns None when it is absent (the platform is then not minimally
# configured and never registers).
_ENV_TABLE = (
    ("RETICULUM_ANNOUNCE_INTERVAL", "announce_interval", float),
    ("RETICULUM_STORAGE_PATH", "storage_path", None),
    ("RETICULUM_RNS_CONFIG_PATH", "rns_config_path", None),
)

# The bridge's name for the storage path. Kept as a fallback so an operator
# migrating an existing bridge .env does not silently get the plugin default
# because the variable was spelled differently. RETICULUM_STORAGE_PATH wins
# when both are set.
_LEGACY_STORAGE_ENV = "RETICULUM_STORAGE"


def _seed_extra() -> dict:
    """``PlatformConfig.extra`` seeded from env, including the legacy key and
    the home channel used for cron delivery to the mesh."""
    seed = _seed_extra_from_env(_ENV_TABLE, home_env="RETICULUM_HOME_CHANNEL")
    if "storage_path" not in seed:
        legacy = (_get_scoped_secret(_LEGACY_STORAGE_ENV, "") or "").strip()
        if legacy:
            seed["storage_path"] = legacy
    return seed


def check_reticulum_requirements() -> bool:
    """Check if RNS and LXMF Python packages are available.

    Kept because the installed plugin shim's ``register(ctx)`` wraps this
    module's ``register`` as the platform ``check_fn``.
    """
    try:
        import LXMF  # noqa: F401
        import RNS  # noqa: F401
        return True
    except ImportError:
        return False


def _validate_config(config) -> bool:
    """Config is always usable — every field has a default."""
    return True


def _is_connected(config) -> bool:
    """Check if the adapter is currently connected.

    Called by the gateway status system; reads through the scoped reader
    (spec section 2: never os.getenv under multiplexing).
    """
    return (_get_scoped_secret("RETICULUM_CONNECTED", "") or "").lower() == "true"


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` and the home channel from env before
    construction, so env-only setups appear in ``hermes gateway status``.
    Build from the (ENV_VAR, extra_key, conv) table via
    ``_shared.seed_extra_from_env`` (spec section 2)."""
    display = (_get_scoped_secret("RETICULUM_DISPLAY_NAME", "") or "").strip()
    if not display:
        # No display name configured: the platform is not minimally set up,
        # matching the IRC example's env_enablement contract.
        return None
    seed = _seed_extra()
    return {"display_name": display, **seed}


def register(ctx):
    """
    Called by the Hermes plugin system to register the Reticulum platform.

    Args:
        ctx: PluginContext with register_platform() method.
    """
    from hermes_reticulum.plugin.adapter import ReticulumPlatformAdapter

    # Build the platform entry. The plugin context OWNS PlatformEntry
    # construction: ctx.register_platform(name, label, adapter_factory,
    # check_fn, **entry_kwargs) builds the dataclass itself and sets
    # source="plugin" / plugin_name for us. So pass plain kwargs — do NOT
    # construct a PlatformEntry here (that raises TypeError: the first
    # positional arg is `name: str`, and unknown keys such as optional_env
    # are not PlatformEntry fields).
    try:
        from gateway.platform_registry import PlatformEntry  # noqa: F401
    except ImportError:
        # If running standalone (not inside gateway), skip registration
        logger.debug("Not running inside Hermes gateway — skipping platform registration")
        return

    ctx.register_platform(
        name="reticulum",
        label="Reticulum (LXMF)",
        adapter_factory=lambda cfg: ReticulumPlatformAdapter(cfg),
        check_fn=check_reticulum_requirements,
        validate_config=_validate_config,
        is_connected=_is_connected,
        env_enablement_fn=_env_enablement,
        required_env=[],
        install_hint="pip install hermes-reticulum",
        emoji="🛜",
        max_message_length=1024,
        pii_safe=True,
        platform_hint=(
            "You are on Reticulum/LXMF (RNode, Sideband, or other mesh client). "
            "Messages are plain text only — no markdown formatting. "
            "Keep responses concise due to bandwidth constraints (especially over LoRa). "
            "The client may be off-grid; route replies efficiently."
        ),
        cron_deliver_env_var="RETICULUM_HOME_CHANNEL",
        allowed_users_env="HERMES_RETICULUM_ALLOWED_USERS",
        allow_all_env="HERMES_RETICULUM_ALLOW_ALL",
    )

    logger.info("Reticulum/LXMF platform registered")
