"""Tool-name → emoji, matching what the Hermes gateway shows on Telegram.

The mesh bridge renders tool activity in three places (step pushes, the
``/tools`` recap, and the control-server summary line). Each used to hardcode
its own glyph, so the same tool could appear as a different emoji depending on
which path delivered it. This module is the single lookup so mesh output lines
up with the gateway's progress bubble.

Source of truth is the gateway's own ``get_tool_emoji()`` (``agent/display.py``),
which resolves a skin override → tool registry → default. We mirror that
resolution order without importing Hermes (the bridge may run in its own venv):
an optional overrides file, then the table below, then a fallback.

Overrides live in ``$HERMES_TOOL_EMOJIS`` (default
``~/.hermes/reticulum_tool_emojis.json``) — a flat ``{"tool_name": "emoji"}``
map. It exists so an operator can re-sync after a Hermes update without waiting
for a bridge release, since the gateway's table grows with new tools.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("hermes_reticulum.tool_emoji")

# Fallbacks, distinct on purpose:
#   UNKNOWN — the tool is not in our table (gateway uses ⚙️ for the same case).
#   ERROR   — the call failed, whatever the tool was.
FALLBACK_EMOJI = "⚙️"
ERROR_EMOJI = "❌"

# Tool name → emoji, copied from the Hermes tool registry (agent/display.py
# get_tool_emoji). Keep names exact; this mirrors the gateway, it does not
# invent a scheme of its own.
TOOL_EMOJIS: dict[str, str] = {
    "annotate_preview": "🔖",
    "apply_layout": "🧱",
    "browser_back": "◀️",
    "browser_cdp": "🧪",
    "browser_click": "👆",
    "browser_console": "🖥️",
    "browser_dialog": "💬",
    "browser_exec": "🌐",
    "browser_get_images": "🖼️",
    "browser_navigate": "🌐",
    "browser_press": "⌨️",
    "browser_scroll": "📜",
    "browser_snapshot": "📸",
    "browser_type": "⌨️",
    "browser_vault_enter_code": "🔐",
    "browser_vault_fill": "🔐",
    "browser_vault_list": "🔐",
    "browser_vault_save_login": "🔐",
    "browser_vault_unlock": "🔐",
    "browser_vision": "👁️",
    "clarify": "❓",
    "close_terminal": "🖥️",
    "cronjob_manage": "⏰",
    "delegate_task": "🔀",
    "desktop_preview": "🖼️",
    "drive_preview": "🖱️",
    "execute_code": "🐍",
    "feishu_doc_read": "📄",
    "feishu_drive_add_comment": "✉️",
    "feishu_drive_list_comment_replies": "💬",
    "feishu_drive_list_comments": "💬",
    "feishu_drive_reply_comment": "✉️",
    "focus_pane": "🪟",
    "gui_tour": "🧭",
    "ha_call_service": "🏠",
    "ha_get_state": "🏠",
    "ha_list_entities": "🏠",
    "ha_list_services": "🏠",
    "image_generate": "🎨",
    "kanban_attach": "📎",
    "kanban_attach_url": "📎",
    "kanban_attachments": "📎",
    "kanban_block": "⏸",
    "kanban_comment": "💬",
    "kanban_complete": "✔",
    "kanban_create": "➕",
    "kanban_heartbeat": "💓",
    "kanban_link": "🔗",
    "kanban_list": "📋",
    "kanban_request_changes": "↩",
    "kanban_request_review": "👀",
    "kanban_show": "📋",
    "kanban_unblock": "▶",
    "manage_connections": "🔗",
    "memory": "🧠",
    "patch": "🔧",
    "process_manage": "⚙️",
    "react_to_message": "💛",
    "read_file": "📖",
    "read_terminal": "🖥️",
    "read_window_below": "🪟",
    "search_files": "🔎",
    "session_search": "🔍",
    "setup_mcp": "🔌",
    "show_tip": "💡",
    "skill_manage": "📝",
    "skill_view": "📚",
    "skills_list": "📚",
    "terminal": "💻",
    "text_to_speech": "🔊",
    "todo_list": "📋",
    "video_analyze": "🎬",
    "video_generate": "🎬",
    "vision_analyze": "👁️",
    "web_extract": "📄",
    "web_search": "🔍",
    "write_file": "✍️",
    "x_search": "🐦",
    # The xai_video_* tools register emoji="video" upstream — a heredoc typo,
    # not a glyph. The mesh renders a real one; the drift check exempts them.
    "xai_video_edit": "🎬",
    "xai_video_extend": "🎬",
    "yb_query_group_info": "👥",
    "yb_query_group_members": "📋",
    "yb_search_sticker": "🔍",
    "yb_send_dm": "✉️",
    "yb_send_sticker": "🎨",
}

_DEFAULT_OVERRIDE_PATH = "~/.hermes/reticulum_tool_emojis.json"

# Parsed once per process; the override file is operator config, not per-turn
# state, and re-reading it on every tool event would put disk I/O in the
# progress path.
_overrides: dict[str, str] | None = None


def _override_path() -> Path:
    return Path(
        os.path.expanduser(os.environ.get("HERMES_TOOL_EMOJIS", _DEFAULT_OVERRIDE_PATH))
    )


def _load_overrides() -> dict[str, str]:
    """Load the operator override map (best-effort; never raises)."""
    global _overrides
    if _overrides is not None:
        return _overrides
    path = _override_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except Exception as exc:  # noqa: BLE001 — bad config must not break tool output
        logger.warning("Ignoring unreadable tool emoji overrides at %s: %s", path, exc)
        raw = {}
    _overrides = {
        str(k): str(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and v
    } if isinstance(raw, dict) else {}
    return _overrides


def reset_cache() -> None:
    """Forget cached overrides (tests, and after an operator edits the file)."""
    global _overrides
    _overrides = None


def tool_emoji(name: str, is_error: bool = False) -> str:
    """Emoji for *name*, mirroring the gateway's resolution order.

    Error calls get :data:`ERROR_EMOJI` regardless of the tool, matching how
    the bridge has always flagged a failed step.
    """
    if is_error:
        return ERROR_EMOJI
    if not name:
        return FALLBACK_EMOJI
    override = _load_overrides().get(name)
    if override:
        return override
    return TOOL_EMOJIS.get(name) or FALLBACK_EMOJI


def tool_label(name: str, is_error: bool = False) -> str:
    """``"<emoji> <tool>"`` — the head line shared by every mesh tool renderer."""
    return f"{tool_emoji(name, is_error)} {name}"
