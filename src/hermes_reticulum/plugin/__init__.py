"""
Hermes Gateway Plugin — registers Reticulum/LXMF as a native platform.

Drop this plugin into ~/.hermes/plugins/reticulum/ and restart the gateway
to enable Reticulum as a first-class messaging platform alongside Telegram,
Discord, WhatsApp, etc.
"""

from hermes_reticulum.plugin.adapter import (
    ReticulumPlatformAdapter,
    check_reticulum_requirements,
)
from hermes_reticulum.plugin.registration import register

# Back-compat alias — the class was renamed to ReticulumPlatformAdapter to
# match the gateway's BasePlatformAdapter contract, but older docs and tests
# still import ``ReticulumAdapter``.
ReticulumAdapter = ReticulumPlatformAdapter

__all__ = [
    "ReticulumAdapter",
    "ReticulumPlatformAdapter",
    "check_reticulum_requirements",
    "register",
]
