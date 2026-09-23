"""
Runtime model-switching for the mesh bridge — the `/model` command.

Mirrors the Telegram gateway's `/model` behaviour but for the LXMF bridge,
where the user talks to the agent over the mesh. A small persisted state file
(`~/.lxmf/state/model.json`) holds the active model so the pin survives a
bridge restart.

The list of switchable models is **discovered at runtime from the active
Hermes config** (``providers.<name>.models`` in ``config.yaml``), never
hardcoded. This keeps the bridge deployment-agnostic (no provider names or
model names baked into shared code) and always in sync with whatever models
the user has actually configured.

Usage (inside ``cli.cmd_run``):

    handler = ModelCommandHandler(hermes)
    # in handle_message:
    if handler.is_command(content):
        return handler.run(content)
"""

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("hermes_reticulum.modelcmd")

# Where the bridge persists the active model pin (lives next to LXMF storage).
DEFAULT_MODEL_STATE_PATH = Path.home() / ".lxmf" / "state" / "model.json"


def discover_models() -> list[str]:
    """
    Discover switchable model names from the active Hermes config.

    Reads two config shapes (both may be present):

    - ``providers.<name>.models`` — list of model names or ``{name: ...}``
      dicts (legacy / hand-written provider blocks).
    - ``custom_providers`` — list of provider entries, each with its own
      ``models`` mapping (the format Hermes auto-writes, e.g. after a
      ``/model`` discovery or provider migration). The entry's default
      ``model`` is included too.

    Returns the flattened list in file order, deduplicated, possibly empty
    (the command then degrades to "list the active model" rather than crash).
    """
    try:
        import yaml
    except ImportError:
        # A missing dependency should not silently masquerade as "no models
        # configured" — that makes /model look broken with no clue. Log loud.
        logger.error(
            "PyYAML is not installed in the bridge environment — "
            "/model cannot read the configured model list. "
            "Run: <venv>/bin/pip install pyyaml"
        )
        return []

    config_path = Path(os.getenv(
        "HERMES_CONFIG",
        str(Path.home() / ".hermes" / "config.yaml"),
    ))
    if not config_path.exists():
        return []
    try:
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning("Could not read Hermes config for model list: %s", e)
        return []

    models: list[str] = []
    seen: set[str] = set()

    def add(name):
        if name and name not in seen:
            seen.add(name)
            models.append(name)

    # Legacy shape: providers.<name>.models = [str | {name: ...}]
    for provider in (data.get("providers") or {}).values():
        if not isinstance(provider, dict):
            continue
        for m in provider.get("models") or []:
            name = m if isinstance(m, str) else (m or {}).get("name")
            add(name)

    # New shape: custom_providers = [{name, model, models: {name: ...}}]
    for provider in data.get("custom_providers") or []:
        if not isinstance(provider, dict):
            continue
        add(provider.get("model"))
        for name in (provider.get("models") or {}):
            add(name)

    return models


class ModelCommandHandler:
    """
    Parses `/model` messages from the mesh and switches the active model.

    The handler is the single place that owns the "active model" for the
    bridge: it writes the pin to disk, pushes it into the HermesClient, and
    answers the user. All other `/model` handling in the pipeline should
    route through this object so state stays consistent.
    """

    def __init__(self, hermes_client, state_path: Path | str | None = None):
        """
        Args:
            hermes_client: The HermesClient instance the bridge uses. Must
                expose `set_model(name)` and `get_model()`.
            state_path: Where to persist the active model. Defaults to
                `~/.lxmf/state/model.json`.
        """
        self.hermes = hermes_client
        self.state_path = Path(state_path or DEFAULT_MODEL_STATE_PATH)
        self._lock = threading.Lock()
        self._active: str | None = self._load()

        # If a persisted pin exists, apply it now so the first message after
        # a restart already uses the right model.
        if self._active:
            self.hermes.set_model(self._active)
            logger.info("Restored persisted model pin: %s", self._active)

    # ── Public API ────────────────────────────────────────────────────

    def is_command(self, content: str) -> bool:
        """Return True if the message is a `/model` control command."""
        return self._parse(content) is not None

    def _parse(self, content: str) -> tuple[str, str | None] | None:
        """
        Parse a `/model` message.

        Returns:
            (action, arg) where action is one of {"set", "list", "reset", "help"}
            and arg is the model name (only for "set"), or None if the message
            is not a /model command.
        """
        if not content:
            return None
        text = content.strip()
        # Accept `/model`, `model`, and leading whitespace.
        body = text
        if body.startswith("/"):
            body = body[1:]
        body = body.strip()
        low = body.lower()
        if low in ("model", "help model"):
            return ("list", None)
        if low.startswith("model "):
            arg = body[len("model"):].strip()
            low_arg = arg.lower()
            if low_arg in ("", "list", "show", "status"):
                return ("list", None)
            if low_arg in ("reset", "default"):
                return ("reset", None)
            if low_arg in ("help", "-h", "--help"):
                return ("help", None)
            return ("set", arg)
        # A bare `model` with nothing after is a list.
        if low == "model":
            return ("list", None)
        return None

    def run(self, content: str) -> str | None:
        """
        Execute a `/model` command and return the reply text (or None if the
        message was not a /model command).
        """
        parsed = self._parse(content)
        if parsed is None:
            return None
        action, arg = parsed

        if action == "help":
            return self._help_text()
        if action == "reset":
            with self._lock:
                self._active = None
                self._persist()
                active = self.hermes.set_model(None)
            return f"Model reset to default ({active})."
        if action == "list":
            return self._list_text()
        if action == "set":
            if not arg:
                return self._list_text()
            return self._set(arg)

        return self._help_text()  # unreachable, but safe

    # ── Internals ─────────────────────────────────────────────────────

    def _set(self, model_name: str) -> str:
        with self._lock:
            self._active = model_name
            self._persist()
            active = self.hermes.set_model(model_name)
        # Cross-check against the configured list, but never block a switch.
        note = ""
        if model_name not in discover_models():
            note = " (not in the configured list — double-check the name)"
        return f"Model set to {active}{note}."

    def _list_text(self) -> str:
        with self._lock:
            active = self._active
        models = discover_models()
        if not models:
            lines = ["Could not read the configured model list."]
        else:
            lines = ["Available models:"]
            for m in models:
                marker = "*" if m == active else " "
                lines.append(f" {marker} {m}")
        lines.append("")
        lines.append(f"Active: {active or '(hermes default)'}")
        lines.append("Use: /model <name>  |  /model  |  /model reset")
        return "\n".join(lines)

    def _help_text(self) -> str:
        return (
            "Model control:\n"
            "  /model <name>   — switch to a configured model\n"
            "  /model          — list available models + show active\n"
            "  /model reset    — clear the pin, use hermes default"
        )

    def _persist(self) -> None:
        """Write the active model to disk (caller holds self._lock)."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"model": self._active}, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.error("Failed to persist model state: %s", e)

    def _load(self) -> str | None:
        """Read the persisted model pin. Returns None if absent/invalid."""
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            model = data.get("model")
            return model if isinstance(model, str) and model.strip() else None
        except (OSError, json.JSONDecodeError):
            return None
