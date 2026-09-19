#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# install.sh — Install Hermes for Reticulum
# ═══════════════════════════════════════════════════════════════
#
# Usage:
#   bash install.sh              # Install with default settings
#   bash install.sh --venv PATH  # Install into specific venv
#   bash install.sh --global     # Install globally (needs pip --break-system-packages)
#   bash install.sh --service    # Also install the systemd user service
#   bash install.sh --no-service # Skip the systemd service step (default: auto)
#
# The --service step renders config/hermes-reticulum.user.service against the
# actual checkout path and current user — no manual path editing required.
# Pass --service to force it on, --no-service to skip it.
#
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
INSTALL_MODE="venv"
INSTALL_SERVICE="auto"

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --venv)
            VENV_DIR="$2"
            INSTALL_MODE="venv"
            shift 2
            ;;
        --global)
            INSTALL_MODE="global"
            shift
            ;;
        --service)
            INSTALL_SERVICE="yes"
            shift
            ;;
        --no-service)
            INSTALL_SERVICE="no"
            shift
            ;;
        --help|-h)
            echo "Usage: bash install.sh [--venv DIR] [--global] [--service] [--no-service]"
            echo "  --venv DIR    Install into a virtual environment (default: ./venv)"
            echo "  --global      Install globally (needs --break-system-packages or pipx)"
            echo "  --service     Install the systemd user service (default: auto — only if systemd user session is available)"
            echo "  --no-service  Skip the systemd service step"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "═══ Hermes for Reticulum — Installer ═══"
echo ""

# ─── Check Python version ───
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        # Portable version extraction (BSD/macOS grep has no -P).
        # `python3 --version` prints e.g. "Python 3.14.7" → take field 2, keep major.minor.
        version=$("$candidate" --version 2>&1 | awk 'NR==1{print $2}' | cut -d. -f1,2)
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PYTHON="$candidate"
            echo "✓ Found Python $version ($candidate)"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "✗ Python 3.11+ required but not found"
    exit 1
fi

# ─── Install ───
if [[ "$INSTALL_MODE" == "venv" ]]; then
    echo ""
    echo "Installing into virtual environment: $VENV_DIR"

    if [[ ! -d "$VENV_DIR" ]]; then
        "$PYTHON" -m venv "$VENV_DIR"
        echo "  Created venv"
    fi

    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip setuptools wheel
    pip install -e "$SCRIPT_DIR"

    echo ""
    echo "✓ Installed into $VENV_DIR"
    echo ""
    echo "  To use:"
    echo "    source $VENV_DIR/bin/activate"
    echo "    hermes-reticulum --help"

else
    echo ""
    echo "Installing globally..."
    pip install --break-system-packages -e "$SCRIPT_DIR"
    echo ""
    echo "✓ Installed globally"
    echo "  Run: hermes-reticulum --help"
fi

# ─── Create config directories ───
echo ""
echo "Setting up configuration..."

RETICULUM_DIR="${HOME}/.reticulum"
LXMF_DIR="${HOME}/.lxmf/storage"

if [[ ! -d "$RETICULUM_DIR" ]]; then
    mkdir -p "$RETICULUM_DIR"
    # Copy example config if not present
    if [[ ! -f "$RETICULUM_DIR/config" && -f "$SCRIPT_DIR/config/reticulum.conf" ]]; then
        cp "$SCRIPT_DIR/config/reticulum.conf" "$RETICULUM_DIR/config"
        echo "  ✓ Created Reticulum config at $RETICULUM_DIR/config"
    fi
else
    echo "  ✓ Reticulum config directory exists"
fi

mkdir -p "$LXMF_DIR"
echo "  ✓ LXMF storage directory ready"

# ─── Copy env file if not present ───
ENV_FILE="${SCRIPT_DIR}/.env"
if [[ ! -f "$ENV_FILE" && -f "$SCRIPT_DIR/config/env.example" ]]; then
    cp "$SCRIPT_DIR/config/env.example" "$ENV_FILE"
    echo "  ✓ Created .env from template"
    echo "    → Edit $ENV_FILE to customize"
fi

# ─── Copy plugins to ~/.hermes/plugins/ ───
echo ""
echo "Setting up Hermes plugins..."

HERMES_PLUGINS_DIR="${HOME}/.hermes/plugins"
mkdir -p "$HERMES_PLUGINS_DIR"

# Reticulum gateway platform plugin. This is a directory-plugin shim that
# bootstraps sys.path to the bridge venv (Hermes and the bridge normally run on
# different interpreters), so we must (re)write venv_path.txt on every install.
RETICULUM_PLUGIN_SRC="$SCRIPT_DIR/plugins/reticulum"
RETICULUM_PLUGIN_DST="$HERMES_PLUGINS_DIR/reticulum"
if [[ -d "$RETICULUM_PLUGIN_SRC" ]]; then
    rm -rf "$RETICULUM_PLUGIN_DST"
    cp -r "$RETICULUM_PLUGIN_SRC" "$RETICULUM_PLUGIN_DST"
    # Two roots: the venv (normal installs) and the repo src/ (editable
    # installs, whose .pth finder is not run when sys.path is patched).
    {
        printf '%s\n' "$VENV_DIR"
        printf '%s\n' "$SCRIPT_DIR/src"
    } > "$RETICULUM_PLUGIN_DST/venv_path.txt"
    echo "  ✓ Installed reticulum plugin to $RETICULUM_PLUGIN_DST/"
    echo "    → bridge venv for the shim: $VENV_DIR"
fi

# Mesh tool gate plugin (pre-execution approval gate). Always refresh so
# upgrades actually take effect.
GATE_PLUGIN_SRC="$SCRIPT_DIR/src/hermes_reticulum/mesh-tool-gate"
if [[ -d "$GATE_PLUGIN_SRC" ]]; then
    rm -rf "$HERMES_PLUGINS_DIR/mesh-tool-gate"
    cp -r "$GATE_PLUGIN_SRC" "$HERMES_PLUGINS_DIR/mesh-tool-gate"
    echo "  ✓ Installed mesh-tool-gate plugin to $HERMES_PLUGINS_DIR/mesh-tool-gate/"
fi

# ─── Enable the plugins in ~/.hermes/config.yaml ───
# Hermes directory plugins are OPT-IN: a plugin only loads when its name is
# listed under `plugins.enabled` in config.yaml. Copying the directories is
# NOT enough — without this step both plugins install silently and never run.
echo ""
echo "Enabling Hermes plugins..."

HERMES_CONFIG="${HOME}/.hermes/config.yaml"
mkdir -p "${HOME}/.hermes"

PY_FOR_YAML=""
if [[ -x "${VENV_DIR}/bin/python" ]]; then
    PY_FOR_YAML="${VENV_DIR}/bin/python"
elif command -v python3 &>/dev/null; then
    PY_FOR_YAML="$(command -v python3)"
fi

if [[ -n "$PY_FOR_YAML" ]]; then
    "$PY_FOR_YAML" - "$HERMES_CONFIG" <<'PYEOF'
import os
import shutil
import sys
import time

path = sys.argv[1]
targets = ["mesh-tool-gate", "reticulum"]

# Fast path: no `plugins:` key at all (the common fresh-install case). Append a
# plain block so the rest of the user's config.yaml (comments included) is
# left byte-for-byte untouched.
text = ""
if os.path.isfile(path):
    with open(path) as f:
        text = f.read()

has_plugins_key = any(
    line.lstrip().startswith("plugins:") for line in text.splitlines()
)

if not has_plugins_key:
    block = "\nplugins:\n  enabled:\n" + "".join(f"    - {t}\n" for t in targets)
    with open(path, "a") as f:
        f.write(block)
    print(f"  ✓ Added plugins.enabled to {path}: {', '.join(targets)}")
    raise SystemExit(0)

# Slow path: a `plugins:` key exists — round-trip through YAML (comments in
# that file will not survive, hence the backup).
try:
    import yaml
except Exception:
    print("  ⚠ PyYAML unavailable — add these to plugins.enabled manually: "
          + ", ".join(targets))
    raise SystemExit(0)

try:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
except Exception as e:
    print(f"  ⚠ Could not parse {path} ({e}) — enable plugins manually: "
          + ", ".join(targets))
    raise SystemExit(0)

if not isinstance(cfg, dict):
    cfg = {}
plugins = cfg.get("plugins")
if not isinstance(plugins, dict):
    plugins = {}
enabled = plugins.get("enabled")
if not isinstance(enabled, list):
    enabled = []

added = [t for t in targets if t not in enabled]
if not added:
    print(f"  ✓ Plugins already enabled in {path}")
    raise SystemExit(0)

enabled.extend(added)
plugins["enabled"] = enabled
cfg["plugins"] = plugins
shutil.copy2(path, f"{path}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
with open(path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)
print(f"  ✓ Enabled plugins in {path}: {', '.join(added)} (backup written)")
PYEOF
else
    echo "  ⚠ No Python found to update config.yaml."
    echo "    Add 'mesh-tool-gate' and 'reticulum' to plugins.enabled manually."
fi

# ─── Systemd user service (optional) ───
echo ""
# Resolve the executable the unit should run. The rendered unit must target
# the environment this install actually put the package in:
#   - venv mode:  $VENV_DIR/bin/hermes-reticulum  (honours --venv PATH)
#   - global:     the interpreter on PATH (no checkout-local venv exists)
if [[ "$INSTALL_MODE" == "venv" ]]; then
    BRIDGE_BIN="${VENV_DIR}/bin/hermes-reticulum"
    BRIDGE_VENV="${VENV_DIR}"
else
    BRIDGE_BIN="$(command -v hermes-reticulum 2>/dev/null || echo hermes-reticulum)"
    BRIDGE_VENV=""
fi
# The unit's WorkingDirectory/EnvironmentFile/Environment(HOME) follow the
# checkout, regardless of where the package was installed.
BRIDGE_HOME="${SCRIPT_DIR}"

# systemd units have no variable expansion and a hostile-looking checkout
# path (|, &, /, whitespace) would corrupt a naive sed substitution, so
# render the unit with the placeholder already resolved via Python.
RENDER_PY="${VENV_DIR}/bin/python"
if [[ ! -x "$RENDER_PY" ]]; then
    RENDER_PY=""
    for cand in python3 python3.13 python3.12 python3.11; do
        if command -v "$cand" &>/dev/null; then
            RENDER_PY="$(command -v "$cand")"
            break
        fi
    done
fi
if [[ -z "$RENDER_PY" ]]; then
    echo "  ⚠ No Python interpreter found — skipping systemd service step."
    echo ""
else
    if [[ "$INSTALL_SERVICE" == "no" ]]; then
        echo "Skipping systemd service (requested via --no-service)"
        echo ""
    elif [[ "$INSTALL_SERVICE" == "yes" ]] || [[ "$INSTALL_SERVICE" == "auto" ]]; then
        # Auto mode: only proceed if a systemd user manager is actually
        # reachable (headless/SSH/container sessions often have systemctl
        # installed but no active user manager).
        if [[ "$INSTALL_SERVICE" == "auto" ]] && \
           { ! command -v systemctl &>/dev/null || ! systemctl --user is-system-running &>/dev/null; }; then
            echo "Skipping systemd service (no reachable systemd user session)"
            echo ""
        else
            UNIT_DIR="${HOME}/.config/systemd/user"
            UNIT_DST="${UNIT_DIR}/hermes-reticulum.service"
            UNIT_SRC="${BRIDGE_HOME}/config/hermes-reticulum.user.service"

            if [[ ! -f "$UNIT_SRC" ]]; then
                echo "✗ Service template not found at $UNIT_SRC"
                exit 1
            fi

            mkdir -p "$UNIT_DIR"
            "$RENDER_PY" - "$UNIT_SRC" "$UNIT_DST" \
                "$BRIDGE_HOME" "$BRIDGE_BIN" "$BRIDGE_VENV" <<'PYEOF'
import sys
src, dst, home, bridge_bin, bridge_venv = sys.argv[1:6]

def _sysd_quote(value):  # systemd-style value quoting (handles whitespace)
    if value and not any(c.isspace() for c in value):
        return value
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

with open(src, encoding="utf-8") as fh:
    lines = fh.readlines()
out = []
for line in lines:
    line = line.replace("/opt/rns_hermes_endpoint", home)
    if line.startswith("ExecStart=") and bridge_bin:
        # bridge_bin may contain whitespace (e.g. a venv path or checkout
        # under a directory with a space); quote it so systemd doesn't split
        # the executable path while parsing ExecStart.
        line = f"ExecStart={_sysd_quote(bridge_bin)} run\n"
    if line.startswith("Environment=HOME="):
        line = f"Environment=HOME={_sysd_quote(home)}\n"
    out.append(line)
    if line.startswith("EnvironmentFile="):
        if bridge_venv:
            out.append(
                f"Environment=PATH={_sysd_quote(bridge_venv + '/bin')}:/usr/bin:/bin\n"
            )
        else:
            out.append("Environment=PATH=/usr/local/bin:/usr/bin:/bin\n")
with open(dst, "w", encoding="utf-8") as fh:
    fh.writelines(out)
PYEOF

            if systemctl --user daemon-reload; then
                if [[ "$INSTALL_SERVICE" == "yes" ]]; then
                    if systemctl --user enable --now hermes-reticulum; then
                        echo "✓ Installed and started systemd user service: hermes-reticulum"
                    else
                        echo "⚗ Wrote and enabled the unit but could not start it."
                        echo "  Check: journalctl --user -u hermes-reticulum"
                        echo "  Retry: systemctl --user start hermes-reticulum"
                    fi
                else
                    if systemctl --user enable hermes-reticulum; then
                        echo "✓ Installed systemd user service: hermes-reticulum (enabled, not started)"
                        echo "  Start it with: systemctl --user start hermes-reticulum"
                        echo "  Follow logs with: journalctl --user -u hermes-reticulum -f"
                    else
                        echo "⚗ Wrote the unit but could not enable it."
                        echo "  Retry: systemctl --user enable hermes-reticulum"
                    fi
                fi
            else
                echo "⚗ Wrote unit to $UNIT_DST but could not talk to systemd."
                echo "  Review it, then run: systemctl --user daemon-reload && systemctl --user enable hermes-reticulum"
            fi
            echo ""
        fi
    fi
fi

# ─── Done ───
echo ""
echo "═══ Installation complete! ═══"
echo ""
echo "Next steps:"
echo "  1. Review and edit .env for your setup."
echo "     The bridge is DENY-BY-DEFAULT: set HERMES_RETICUM_ALLOWED_USERS to the"
echo "     LXMF hashes allowed to talk to the agent (or set"
echo "     HERMES_RETICUM_ALLOW_ALL=true to open it to any mesh peer)."
echo "  2. Restart the Hermes gateway so the newly enabled plugins load."
echo "  3. Start the bridge:  hermes-reticulum run"
echo "  4. Note the LXMF address printed on startup"
echo "  5. Add this address as a contact in Sideband (Android)"
echo ""
echo "Note: the pre-execution gate waits MESH_GATE_TIMEOUT seconds (default 900)"
echo "for your /approve verdict and fails closed on timeout. See README.md."
echo ""
