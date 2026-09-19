# Hermes for Reticulum

**Put an AI agent on your Reticulum mesh — reachable from off-grid RNodes, Sideband, and any LXMF client, without a central messaging server.**

You deploy one bridge node. Field hardware sends encrypted LXMF messages over LoRa, TCP, or I2P. Reticulum routes across hops until Hermes Agent processes the request and the reply travels back the same mesh path. Clients in the field do not need direct internet access.

```
RNode / Sideband / any LXMF client          Hermes bridge (Reticulum node)
┌──────────────────────────┐   LoRa / TCP / I2P   ┌──────────────────────────┐
│  Off-grid mesh client    │ ◄════ Reticulum ═══► │  Hermes for Reticulum    │
│  (no internet required)  │   LXMF (encrypted)   │  └── Hermes Agent (AI)   │
└──────────────────────────┘                      └──────────────────────────┘
         ▲                                                    │
         │         mesh hops through Reticulum peers          │
         └──────── (field nodes, gateways, TCP/I2P bridges) ──┘
```

## Who this is for

| Persona | What you need | What this gives you |
|---------|---------------|---------------------|
| **Mesh developer** | A standard LXMF endpoint you can hit from Python, Sideband, or custom tooling | A drop-in bridge — no proprietary API, no Sideband lock-in |
| **Systems architect** | AI capability at the edge of a heterogeneous mesh (LoRa + TCP + I2P) | One gateway pattern: mesh clients stay off-grid; the bridge holds the uplink to LLM APIs |
| **Field operator** | Reliable comms where cellular and Wi-Fi fail | Ask your agent from an [RNode](https://github.com/markqvist/Reticulum) in the bush; replies route back over the same mesh |

If you design or operate off-grid communication systems, this is the missing link between **Reticulum's transport layer** and **Hermes Agent's reasoning layer**.

## The problem this solves

Off-grid mesh nodes can move data. They usually cannot reach a capable AI without brittle workarounds — custom gateways, ad-hoc HTTP tunnels, or forcing every client to carry its own internet link.

Hermes for Reticulum closes that gap:

- **What:** an LXMF bridge that forwards mesh messages to [Hermes Agent](https://github.com/NousResearch/hermes-agent) and returns replies over [Reticulum](https://reticulum.network).
- **Why:** field nodes should talk to an agent through the mesh they already trust — not through a new protocol stack.
- **Where:** RNodes in remote terrain, event meshes with no ISP, hybrid networks where LoRa peers reach a TCP/I2P gateway.
- **How much effort:** under 10 minutes to a running bridge — see [QUICKSTART.md](QUICKSTART.md).

## What you get

- **AI reachable from off-grid mesh nodes** — especially [RNodes](https://github.com/markqvist/Reticulum) on LoRa; no Android or Sideband required
- **Client-agnostic LXMF** — [Sideband](https://github.com/markqvist/Sideband), NomadNet, custom Python apps, or any Reticulum peer that speaks LXMF
- **Reticulum-native routing** — LoRa, TCP, I2P, and other transports interoperate on one network
- **End-to-end encryption** — Curve25519 key exchange + AES-128; Ed25519 message signatures
- **No central messaging server** — identity-based LXMF delivery across autonomous peers
- **Internet where it belongs** — the bridge node (often a VPS or home gateway) reaches LLM APIs; mesh clients do not need a direct ISP link
- **Access control by LXMF identity hash** — allowlist or blocklist senders before they reach Hermes

## Real-world scenarios

| Scenario | Challenge | Outcome with this bridge |
|----------|-----------|--------------------------|
| **RNode in the field** | No cellular coverage; operator needs situational answers | Message goes out over LoRa; Reticulum forwards through mesh peers; Hermes replies on the return path |
| **Off-grid camp or event** | Local LoRa mesh, no ISP on site | One gateway node with TCP or I2P reachability acts as the AI endpoint for the whole mesh |
| **Hybrid mesh** | Remote nodes on LoRa; infrastructure on TCP | Bridge on a VPS joins both worlds; Hermes uses cloud LLMs while clients stay radio-only |
| **Sideband on a phone** | Mobile operator wants the same agent contact | Same LXMF address — convenient client, not a requirement |

## Quick start

Follow [QUICKSTART.md](QUICKSTART.md) to go from clone to first mesh message in under 10 minutes.

```bash
git clone https://github.com/strtPath/rns_hermes_endpoint.git
cd rns_hermes_endpoint
bash install.sh && source venv/bin/activate
cp config/env.example .env   # set HERMES_BIN and allowed LXMF hashes
hermes-reticulum run           # note the LXMF address printed at startup
```

## Requirements

| Item | Version / detail |
|------|------------------|
| Python | 3.11 or newer (Linux and macOS; install.sh avoids GNU-only utilities) |
| Hermes Agent | Installed and working (`hermes chat -q "reply OK only"`). Verified against Hermes **v0.19.0**; the bridge adapts to Hermes CLI flag/schema differences automatically |
| Bridge node (optional) | Public IP with TCP port **37428** open, if internet-connected Reticulum peers should reach you |

`install.sh` does more than create the venv: it copies both Hermes plugins
(`reticulum`, `mesh-tool-gate`) into `~/.hermes/plugins/` **and** adds them to
`plugins.enabled` in `~/.hermes/config.yaml`. Hermes directory plugins are
opt-in, so an install that only copied the directories would never load them.
Restart the Hermes gateway afterwards so they activate.

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/strtPath/rns_hermes_endpoint.git
cd rns_hermes_endpoint

# 2. Install (creates venv and dependencies)
bash install.sh

# 3. Activate the virtual environment
source venv/bin/activate

# 4. Configure environment variables
cp config/env.example .env
# Edit .env for your deployment
#   - HERMES_RETICUM_ALLOWED_USERS: LXMF hashes allowed to talk to the agent
#     (the bridge is DENY-BY-DEFAULT; without an allowlist entry, senders are
#      rejected — set HERMES_RETICUM_ALLOW_ALL=true only if you mean it)
#   - HERMES_BIN: only needed if `hermes` is not on PATH

# 5. Configure Reticulum (TCP Server interface)
#    The installer copies config/reticulum.conf to ~/.reticulum/config if missing.
#    Adjust listen_port and, if needed, the TCP Client peer:
#
#    [[TCP Server Interface]]
#      type = TCPServerInterface
#      listen_ip = 0.0.0.0
#      listen_port = 37428
#
#    [[TCP Client]]
#      target_host = YOUR_MESH_PEER_HOST
#      target_port = YOUR_MESH_PEER_PORT

# 6. Open the firewall port (if exposing the node to the internet)
sudo ufw allow 37428/tcp

# 7. Start the bridge
hermes-reticulum run

# 8. Note the LXMF address printed at startup
#    Add it as a contact in Sideband, your RNode config, or any LXMF client
```

## Commands

```bash
hermes-reticulum run              # Start the bridge
hermes-reticulum run --verbose    # Verbose logging
hermes-reticulum address          # Show LXMF address
hermes-reticulum status           # Bridge status

# Custom options
hermes-reticulum run \
  --display-name "My Agent" \
  --stamp-cost 4 \
  --timeout 120 \
  --hermes-bin /path/to/hermes
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Mesh clients (any LXMF-capable peer)                   │
│  ├── RNode + LoRa radio (primary off-grid use case)     │
│  ├── Sideband (Android / desktop)                       │
│  └── Custom LXMF apps / other Reticulum nodes           │
│       └── Reticulum stack → LoRa / TCP / I2P / …        │
└─────────────────────┬───────────────────────────────────┘
                      │ LXMF messages (encrypted), multi-hop
                      ▼
┌─────────────────────────────────────────────────────────┐
│  Bridge node (VPS, home server, or mesh gateway)        │
│  ┌─────────────────────────────────────────────────┐    │
│  │  Reticulum daemon (TCP :37428, LoRa, I2P, …)    │    │
│  └────────────────────┬────────────────────────────┘    │
│                       ▼                                 │
│  ┌─────────────────────────────────────────────────┐    │
│  │  Hermes for Reticulum (this project)            │    │
│  │  ├── LXMFBridge      — LXMF message handling    │    │
│  │  ├── HermesClient    — hermes CLI subprocess    │    │
│  │  └── AccessControl   — sender filtering         │    │
│  └────────────────────┬────────────────────────────┘    │
│                       ▼                                 │
│  ┌─────────────────────────────────────────────────┐    │
│  │  Hermes Agent (LLM, tools, memory, skills)      │    │
│  │  └── may use internet for model APIs            │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

Reticulum peers interconnect autonomously. An off-grid RNode only needs a path — direct or multi-hop — to the bridge. TCP and I2P interfaces on gateway nodes extend the mesh to internet-connected peers without requiring every client to have an ISP link.

## How it works

1. A mesh client (RNode, Sideband, or any LXMF peer) sends a message to the bridge hash
2. Reticulum routes it across the mesh — LoRa hops, TCP links, I2P tunnels, or a mix
3. LXM Router validates the signature and decrypts the payload
4. A thread pool processes the message in a worker (non-blocking — long Hermes calls do not stall the mesh stack)
5. AccessControl checks the sender against your allowlist
6. HermesClient runs `hermes chat -q "<message>"` (Hermes may use internet on the bridge node)
7. The reply is sent back as an LXMF message to the originating client

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RETICULUM_DISPLAY_NAME` | `Hermes for Reticulum` | Name shown on the mesh |
| `RETICULUM_STORAGE` | `~/.lxmf/storage` | LXMF storage path |
| `RETICULUM_STAMP_COST` | `8` | Stamp cost (bandwidth throttle) |
| `RETICULUM_CONFIG` | `~/.reticulum` | Reticulum config directory |
| `HERMES_BIN` | *(auto-detected)* | Path to the `hermes` binary |
| `HERMES_TIMEOUT` | `300` | Hermes timeout (seconds) |
| `HERMES_RETICUM_ALLOW_ALL` | `false` | Allow any sender |
| `HERMES_RETICUM_ALLOWED_USERS` | *(empty)* | LXMF hash allowlist |
| `HERMES_RETICUM_BLOCKED_USERS` | *(empty)* | LXMF hash blocklist |
| `HERMES_TOOL_EMOJIS` | `~/.hermes/reticulum_tool_emojis.json` | Optional tool-emoji override map (see below) |
| `MESH_GATE_TRIAGE` | `off` | Jev pre-gate triage stage: `off`, `hint_only`, or `allow_benign` (see below) |
| `MESH_GATE_TRIAGE_CONF` | `0.6` | Confidence floor for auto-allow under `allow_benign` |
| `MESH_GATE_TRIAGE_EXEC` | `0` | **Reserved/inert** — `execute_code` is ALWAYS human-gated (opaque payload; triage may deny it but never auto-allow it) |

Full template: [config/env.example](config/env.example).

### Jev pre-gate triage

With `MESH_GATE_TRIAGE=allow_benign` and an `OPENROUTER_API_KEY` (or
`TYPESAFE_API_KEY`) in the gateway env, Jev may auto-allow routine,
benign tool calls before the human approve/deny gate is offered.
Anything sensitive, destructive, uncertain, or below
`MESH_GATE_TRIAGE_CONF` still escalates to the human gate, and any Jev
error or timeout fails open to the human gate. `hint_only` logs the
classification without changing the gate (dry-run calibration).

`MESH_GATE_TRIAGE_CONF` defaults to `0.6`. Calibrated 2026-09-18 against
13 representative calls with `typesafe/jev-1.13`; the 0.4-0.7 range is a
flat plateau where the same five read-only calls auto-clear and nothing
sensitive or destructive clears at any tested floor. See
[docs/mesh-gate-triage-confidence-floor.md](docs/mesh-gate-triage-confidence-floor.md)
for the full rationale and the recommendation to run `hint_only` for a
few days of real traffic before flipping to `allow_benign`.

`execute_code` is an exception worth calling out: its opaque Python payload
cannot be reliably classified from a truncated first-line excerpt, and raw code would
leak to the provider, so it is ALWAYS routed through the human gate. Jev triage may
still DENY a clearly destructive `execute_code` call, but it can never auto-ALLOW one.

### Tool emojis

Tool activity on the mesh uses the same per-tool emoji the Telegram gateway
shows (`📖 read_file`, `💻 terminal`, `🔍 web_search`, `🐍 execute_code` …), so a
transcript reads the same whichever platform the operator is on. The table ships
with the bridge, and a failed call is always `❌`.

The table is a copy of the gateway's registry, which grows as Hermes adds tools.
To re-sync without waiting for a bridge release, drop a flat
`{"tool_name": "emoji"}` map at `~/.hermes/reticulum_tool_emojis.json`
(override with `HERMES_TOOL_EMOJIS`); entries there win over the built-in table.
An unreadable override file is logged and ignored rather than fatal.

```json
{
  "terminal": "🖥",
  "some_new_tool": "🛰"
}
```

### Hermes binary detection

Automatic search order:

1. `hermes` on `PATH`
2. `/opt/hermes/.venv/bin/hermes`
3. `/opt/hermes/bin/hermes`
4. `~/.hermes/bin/hermes`
5. `~/.local/bin/hermes`

Override with `--hermes-bin` or the `HERMES_BIN` variable.

### Access control

By default, only allowlisted addresses can interact (`HERMES_RETICUM_ALLOW_ALL=false`).

```bash
# In .env
HERMES_RETICUM_ALLOW_ALL=false
HERMES_RETICUM_ALLOWED_USERS=your_client_lxmf_hash,optional_second_hash
```

Each client has a 32-character hex LXMF identity hash — from Sideband (Settings → Identity), your RNode/Reticulum identity, or `hermes-reticulum address` on the bridge itself.

## Systemd service

`install.sh` renders the unit from your checkout — no manual path editing.
The template in `config/` uses the placeholder path `/opt/rns_hermes_endpoint`;
the installer resolves it to your actual repo path, points `ExecStart` at the
binary the install actually created (honouring `--venv` / `--global`), injects
a `PATH` env line (user units inherit a minimal environment), writes
`~/.config/systemd/user/hermes-reticulum.service`, and enables it.

### User-level (no root)

```bash
# Installs the package and the systemd user service in one step:
bash install.sh --service

# Or, if you already installed the package, render the unit by hand:
python3 - <<'EOF'
import os
home = os.getcwd()
src = os.path.join(home, "config", "hermes-reticulum.user.service")
dst = os.path.expanduser("~/.config/systemd/user/hermes-reticulum.service")
os.makedirs(os.path.dirname(dst), exist_ok=True)
lines = open(src).readlines()
out, venv = [], os.path.join(home, "venv")
for line in lines:
    line = line.replace("/opt/rns_hermes_endpoint", home)
    if line.startswith("ExecStart="):
        line = f"ExecStart={venv}/bin/hermes-reticulum run\n"
    out.append(line)
    if line.startswith("EnvironmentFile="):
        out.append(f"Environment=PATH={venv}/bin:/usr/bin:/bin\n")
open(dst, "w").writelines(out)
EOF
systemctl --user daemon-reload
systemctl --user enable --now hermes-reticulum
journalctl --user -u hermes-reticulum -f
```

### System-level (root)

```bash
# The unit ships with the /opt/rns_hermes_endpoint placeholder and a
# dedicated User=/Group= (hermes:hermes) for hardened deployments.
# Adjust both for your deployment before installing:
sudo cp config/hermes-reticulum.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-reticulum
```

## Connect from the mesh

Any LXMF client on Reticulum can reach the bridge. Add the bridge's LXMF address (`hermes-reticulum address`) as a contact or destination, then send a plain-text message.

### RNode (recommended for off-grid)

1. Run Reticulum on the RNode with a LoRa interface linked to your mesh
2. Ensure the RNode has a Reticulum path to the bridge (direct LoRa, or via TCP/I2P gateway peers)
3. Send an LXMF message to the bridge hash from your RNode tooling or LXMF client
4. The agent reply routes back over the same mesh

This is the primary scenario: **field hardware with no internet**, talking to an AI through Reticulum hops.

### Sideband (Android / desktop)

1. Install [Sideband](https://github.com/markqvist/Sideband/releases/latest)
2. Configure a network interface (Wi-Fi/TCP or LoRa)
3. Add the bridge's LXMF address as a contact
4. Send a message — the agent replies via Hermes

### Other LXMF clients

NomadNet, custom Python scripts using the [LXMF](https://github.com/markqvist/lxmf) library, or any Reticulum node configured for LXMF delivery can interact with the same bridge address. No Sideband-specific features are required.

## Deployment

### Pre-execution gate and timeout configuration

The bridge includes a pre-execution approval gate: when the agent
attempts a dangerous command, the operator on the mesh is prompted to
`/approve` or `/deny` before the tool runs. The gate waits up to
`MESH_GATE_TIMEOUT` seconds (default 900) for the operator's verdict, and
**fails closed** — an unanswered gate blocks the tool.

Because Hermes invokes `pre_tool_call` hooks **synchronously** (see
`hermes_cli/plugins.py::invoke_hook`), the gate blocks the turn for up to
`MESH_GATE_TIMEOUT` seconds while it waits for your reply. Meanwhile the
bridge's liveness guard keeps the child turn alive during the wait, so a long
gate does not trip it.

**Three timeouts must stay aligned** (or the mesh gate wedges when the
operator is AFK). The gate has three stacked layers — the control-server deny
clock (`HERMES_MESH_APPROVAL_TIMEOUT`), the plugin's blocking POST
(`MESH_GATE_TIMEOUT`), and Hermes' hook-callback timeout
(`plugins.hook_callback_timeout`, a `~/.hermes/config.yaml` setting, default
30s, hard max 600s). `pre_tool_call` is a **fail-closed** hook, and after a
timeout Hermes suppresses re-firing it for 60s — so if the hook-callback
timeout fires before the control clock resolves, EVERY tool fails with
`pre_tool_call plugin callback timed out or is still running` for the rest of
the turn. The layers MUST satisfy `MESH_GATE_TIMEOUT >=
HERMES_MESH_APPROVAL_TIMEOUT` and `hook_callback_timeout > MESH_GATE_TIMEOUT`
and `hook_callback_timeout < 600`.

> **⚠️ The shipped code defaults are NOT safe on their own.** The repo
> defaults for the two bridge timeouts are 900s each, and Hermes ships
> `hook_callback_timeout` at 30s. Left untouched, the 30s hook wrapper fires
> far before the 900s gate resolves — so if the operator is AFK past 30s,
> `pre_tool_call plugin callback timed out or is still running` wedges every
> tool for the rest of the turn. **You MUST set all three** on deployment:
> choose a gate window, then set `MESH_GATE_TIMEOUT` and
> `HERMES_MESH_APPROVAL_TIMEOUT` to that value in the bridge `.env`, and
> `plugins.hook_callback_timeout` above it (and below 600) in
> `~/.hermes/config.yaml`. The 480/480/490 alignment on this host is one such
> valid choice; it is an example, not the shipped default. Raising the gate
> toward 900s is pointless — the 600s hard clamp in Hermes core caps the real
> operator window at ~590s no matter what you set. Since v0.21.x the bridge
> also **refuses to start** if `MESH_GATE_TIMEOUT <
> HERMES_MESH_APPROVAL_TIMEOUT` (it would fail closed before you could ever
> answer), so keep the ordering correct.

A concrete safe recipe for a fast link (WiFi/LAN):

| Setting | Where | Purpose |
|---------|-------|---------|
| `MESH_GATE_TIMEOUT` | bridge `.env` | How long the gate waits for your verdict (default 900s). Must be `>= HERMES_MESH_APPROVAL_TIMEOUT`. |
| `HERMES_MESH_APPROVAL_TIMEOUT` | bridge `.env` | Control-server-side gate wait (default 900s). |
| `plugins.hook_callback_timeout` | `~/.hermes/config.yaml` | Hermes hook-wrapper backstop (default 30s, hard max 600s). Must be `> MESH_GATE_TIMEOUT` and `< 600`. |
| `HERMES_MESH_CONTROL_URL` | bridge `.env` / plugin env | Where the gate plugin posts `/gate/notify` (default `http://127.0.0.1:8471`) |

If your Hermes build *does* expose a hook timeout that is shorter than
`MESH_GATE_TIMEOUT`, the shorter value wins and the tool is blocked before your
verdict arrives — so raise it to at least `MESH_GATE_TIMEOUT`. Verify what your
build supports with `hermes config get plugins` before relying on it.

Full details: [docs/pre-tool-callback-timeout-issue.md](docs/pre-tool-callback-timeout-issue.md).

### Clean replies on the mesh (turn off reasoning recap)

Hermes renders a "Reasoning" recap panel and, in some environments, a scanner
notice line. With `display.show_reasoning: true` (the Hermes default) those
appear in the child's output and are forwarded verbatim to the mesh — so a
LoRa/Sideband client receives box-drawing art before the actual answer.

For a mesh deployment, disable the recap in `~/.hermes/config.yaml`:

```yaml
display:
  show_reasoning: false
```

This is a Hermes display setting, not a bridge setting. If you would rather
keep reasoning on your local CLI, run the bridge with a dedicated Hermes
profile (`HERMES_HOME`) that has `show_reasoning: false`.

## Development

```bash
git clone https://github.com/strtPath/rns_hermes_endpoint.git
cd rns_hermes_endpoint

python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

python -m pytest tests/ -v
ruff check src/ tests/
```

## Security

- End-to-end encryption (Curve25519 + AES-128)
- Forward secrecy via ephemeral links
- Ed25519 message signatures
- Access control by identity hash
- Thread pool limits concurrent processing
- The LXMF hash is public on the mesh (like a contact identifier)

## Get started

Clone the repo, run the bridge, add your mesh clients to the allowlist, and send your first off-grid message. Step-by-step: [QUICKSTART.md](QUICKSTART.md).

## License

MIT

## Acknowledgments

- [Reticulum](https://reticulum.network) — mesh networking stack (RNode, transports, routing)
- [LXMF](https://github.com/markqvist/lxmf) — messaging protocol
- [Sideband](https://github.com/markqvist/Sideband) — LXMF client (one of many)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — AI agent
- [rns_hermes_endpoint](https://github.com/apolosan/rns_hermes_endpoint) — the original project this bridge was forked from
