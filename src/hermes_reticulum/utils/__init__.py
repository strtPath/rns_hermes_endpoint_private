"""Utility modules for Hermes for Reticulum."""

from __future__ import annotations

import os
from typing import overload


@overload
def expand_path(value: None) -> None: ...


@overload
def expand_path(value: str | os.PathLike[str]) -> str: ...


def expand_path(value: str | os.PathLike[str] | None) -> str | None:
    """Normalize a user-supplied path from config/env.

    ``.env`` values are read verbatim, so a documented default like
    ``RETICULUM_STORAGE=~/.lxmf/storage`` arrives as the literal string
    ``"~/.lxmf/storage"`` unless we expand it. Without this, the bridge creates
    a directory *named* ``~`` relative to the current working directory while
    other components (which do expand) use ``$HOME`` — so the identity and the
    control token end up in different places.

    Expands ``~`` and ``$VARS``. Returns ``None`` for ``None``.
    """
    if value is None:
        return None
    return os.path.expanduser(os.path.expandvars(str(value)))


__all__ = ["expand_path"]
