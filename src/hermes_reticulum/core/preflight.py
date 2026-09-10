"""Startup preflight checks for the Hermes Reticulum bridge."""

import os
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class PreflightResult:
    ok: bool
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        for c in self.checks:
            lines.append(f"  ✓ {c}")
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        for e in self.errors:
            lines.append(f"  ✗ {e}")
        if not self.ok:
            lines.append("")
            lines.append("The bridge cannot start until the errors above are fixed.")
            lines.append("See README.md → Deployment for setup details.")
        return "\n".join(lines)


def run_preflight(
    hermes_bin: str | None = None,
    storage: str | None = None,
    config_yaml: str | None = None,
    check_plugins: bool = True,
) -> PreflightResult:
    """Run all preflight checks and return a structured result.

    Args:
        hermes_bin: Explicit hermes binary path. Auto-detected if None.
        storage: Storage path. Defaults to RETICULUM_STORAGE env or ~/.lxmf/storage.
        config_yaml: Path to Hermes config.yaml. Auto-detected if None.
        check_plugins: Whether to check for mesh-tool-gate plugin presence.
    """
    result = PreflightResult(ok=True)

    # 1. Hermes binary
    if hermes_bin is None:
        from hermes_reticulum.core.hermes_client import find_hermes_bin
        hermes_bin = find_hermes_bin()
    elif not os.path.isabs(hermes_bin) and not os.path.isfile(hermes_bin):
        # Bare name (e.g. "hermes") or relative path — resolve via PATH,
        # exactly as HermesClient/find_hermes_bin do, before judging it.
        from hermes_reticulum.core.hermes_client import find_hermes_bin
        hermes_bin = find_hermes_bin() or hermes_bin

    if hermes_bin is None:
        result.errors.append(
            "Hermes binary not found. Install Hermes Agent or set "
            "HERMES_BIN in .env to the path of the hermes CLI."
        )
        result.ok = False
    elif not os.path.isfile(hermes_bin):
        result.errors.append(
            f"Hermes binary {hermes_bin} does not exist. "
            "Fix HERMES_BIN in .env or install Hermes Agent."
        )
        result.ok = False
    else:
        result.checks.append(f"Hermes binary: {hermes_bin}")
        # Verify it actually runs
        try:
            r = subprocess.run(
                [hermes_bin, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            version_line = (r.stdout or r.stderr).strip().split("\n")[0]
            result.checks.append(f"Hermes version: {version_line}")
        except (subprocess.TimeoutExpired, OSError) as e:
            result.warnings.append(
                f"Hermes binary exists but --version timed out or failed: {e}"
            )

    # 2. Storage path
    storage = storage or os.getenv("RETICULUM_STORAGE") or os.path.expanduser("~/.lxmf/storage")
    storage = os.path.expanduser(storage)
    if not os.path.isdir(storage):
        result.warnings.append(
            f"Storage directory {storage} does not exist yet — it will be created on first run."
        )
    else:
        identity_path = os.path.join(storage, "hermes_identity")
        if os.path.exists(identity_path):
            result.checks.append(f"Identity: {identity_path}")
        else:
            result.warnings.append(
                f"No identity at {identity_path} — one will be generated on first run."
            )

    # 3. Hermes config.yaml
    if config_yaml is None:
        config_yaml = os.getenv("HERMES_CONFIG") or os.path.expanduser("~/.hermes/config.yaml")
    config_yaml = os.path.expanduser(config_yaml)
    if os.path.isfile(config_yaml):
        result.checks.append(f"Config: {config_yaml}")
    else:
        result.warnings.append(
            f"Hermes config not found at {config_yaml} — model discovery and "
            "plugin settings will use defaults."
        )

    # 4. Plugin presence (optional)
    if check_plugins:
        plugins_dir = os.path.expanduser("~/.hermes/plugins")
        gate_plugin = os.path.join(plugins_dir, "mesh-tool-gate")
        if os.path.isdir(gate_plugin):
            result.checks.append("Plugin: mesh-tool-gate")
        else:
            result.warnings.append(
                "mesh-tool-gate plugin not found at ~/.hermes/plugins/ — "
                "pre-execution approval gate will be disabled."
            )

    return result
