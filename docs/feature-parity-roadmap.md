# Mesh Bridge — Feature Parity Roadmap (vs Telegram gateway)

_Goal: bring the Reticulum/LXMF mesh bridge (`rns_hermes_endpoint`) to feature
parity with the Hermes **Telegram** gateway, so the mesh endpoint is a first-class
way to talk to the agent, not a degraded one. This is a living doc — update the
Status column as we land items._

_Last updated: 2026-09-12. Owner: Holo + user._

---

## What "feature parity" means here

The Telegram gateway (inside Hermes) already exposes a rich command surface and
behavior set. The mesh bridge currently exposes a subset. The roadmap below is
the gap, grouped into four tiers by effort/value:

1. **Foundations** — reliability + continuity (fix what's flaky first)
2. **Command parity** — match the Telegram `/` command set
3. **Interaction parity** — match Telegram's non-command behaviors (confirmations,
   attachments, voice, streaming)
4. **Ops / hardening** — make it safe to leave running and safe to PR upstream

> **Note on sources:** items marked *(verified this session)* were confirmed
> against the live bridge/log/DB. Items marked *(verify vs Telegram source)*
> need a one-line check against the actual Telegram gateway handler before
> we commit to them — the Telegram source wasn't inspected in this session, so
> treat that list as the known surface, not a proven gap.

---

## Tier 1 — Foundations (do first; everything else rides on this)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1.1 | **True session continuity** | ✅ done | Resolves thread by title via direct `state.db` query (no subprocess). Fix committed 2026-08-21. |
| 1.2 | **Model responsiveness guard** | ✅ done | Liveness watchdog: kills `hermes` if zero stdout+stderr output for `HERMES_LIVENESS_TIMEOUT` (default 180s). `/stop` command kills manually. Pairs with slow 27b — threshold is configurable. |
| 1.3 | **`/model` runtime discovery** | ✅ done | `model_command.py` now reads models from `config.yaml` at runtime (no hardcoded catalog). *(verified this session.)* |
| 1.4 | **`/new`, `/help`, `/commands`** | ✅ done | `CommandDispatcher` in `commands.py`; `/new` confirmed via log `Session reset — new thread: mesh-reticulum-<ts>`. *(verified this session.)* |
| 1.5 | **Startup loud-fail on missing `hermes`** | ✅ done | `core/preflight.py` — `run_preflight()` checks binary (+`--version`), storage, config, plugins. CLI `run` logs all checks; missing binary → `sys.exit(1)` before RNS starts. Mesh `/status` shows `Preflight: ✓ ok` or `✗ N error(s)` with the error lines. CLI `status` subcommand renders the full preflight. |
| 1.6 | **SIGTERM clean exit** | ✅ done | `_handle_signal()` → daemon thread → `_clean_exit()`: `stop()` → `RNS.exit(0)`, `os._exit(0)` last resort. Verified: `kill -TERM` exits within 3s (was: indefinite hang). Commit `36d94bb`. |
| 1.7 | **Downlink acks / RSSI/SNR profiler** | ✅ acks done / ⬜ profiler | **Downlink acks (v2)** re-landed 2026-09-12 (commit `b07bc90`): per-chunk `register_delivery_callback`, `[p<N> i/N]` sequence tagging, 500ms pacing (failed-send-safe), ack-timeout sweep (`HERMES_DOWNLINK_ACK_TIMEOUT_S`, default 300s). Live-verified on TCP: `Downlink ack seq=1 → ... state=delivered (DELIVERED)` in 1s. A–F regression fixes documented in `docs/mesh-bridge-findings-2026-09-12-reminder-tool-calls-never-arrived.md`. **RSSI/SNR profiler** (reading `RNS.Transport.local_client_rssi_cache`) still open — separate concern, needs a real RNode radio to populate the cache. |
| 1.8 | **Liveness heartbeat (generation-scoped)** | ✅ done | Per-child generation counter; marker requires session match + gen match + mtime. Slow-but-working child stays warm; genuinely wedged child still killed. Commit `a154d7c`. |

---

## Tier 2 — Command parity (match the Telegram `/` set)

**Verified against the real registry** (`hermes_cli/commands.py` →
`COMMAND_REGISTRY`, read 2026-08-20). A command is available over a gateway
platform (Telegram *and* the mesh) unless it is marked `cli_only=True`. So the
"feature parity" target is every non-`cli_only` command below.

### Already done on the mesh
| Command | Status |
|---------|--------|
| `/model` | ✅ (runtime discovery) |
| `/new` (alias `/reset`) | ✅ |
| `/help` | ✅ |
| `/commands` | ✅ |
| `/stop` | ✅ (kills running hermes subprocess; pairs with 1.2 liveness guard) |
| `/status` | ✅ (model, session, process, tokens, timeouts) |
| `/pause` / `/resume` | ✅ (global emergency stop via `hermes pause`/`resume`) |
| `/retry` | ✅ (resends last prompt) |
| `/usage` | ✅ (session-local token totals from state.db) |
| `/version` | ✅ (`hermes --version`) |
| `/whoami` | ⏸️ deferred (needs ACL access-level plumbing) |
| `/approve` / `/deny` | ✅ done (2026-08-22, via mesh control server + agent:step hook; see findings doc) |
| `/steer` | ✅ done (2026-08-22, queues text consumed by next `chat()`) |
| `/tools` | ✅ done (2026-08-22, per-turn tool recap from control server / state.db) |
| `/verbose` | ✅ done (2026-08-22, toggles detailed tool recaps) |
| `/steps on\|off` | ✅ done (2026-08-22, step-through mode: full tool call + full output chunked across multiple LXMF posts; model instructed to announce each tool before running it) |
| `/hold` / `/go` | ✅ done (2026-08-22, checkpoint gate: holds the final reply until /go or 30-min timeout; auto-releases) |

### Not `cli_only` — in parity scope (grouped, with mesh priority)

**Model selection — provider-level (follow-up):**
| Item | Status | Notes |
|------|--------|-------|
| `/model` lists all providers | ⏸️ backlog | Currently only surfaces `custom_providers` (HecateV). Should also list `fallback_providers` (e.g. the OpenRouter entry) and the top-level default, grouped by provider. Requires `set_model` to pin **provider + model** (not just model name) so the pin survives and targets the right provider. |

**High value — do next (cheap + closes real pain):**
| Command | Status | Why it matters for mesh |
|---------|--------|-------------------------|
| `/status` | ✅ done | Self-diagnosis over mesh (also Tier 4.3) |
| `/stop` | ✅ done | Escapes a hung 27b run (pairs with 1.2 liveness guard) |
| `/pause` | ✅ done | Mesh-specific safety valve |
| `/approve` / `/deny` | ✅ done | The gateway's approval model over mesh |
| `/retry` | ✅ done | Cheap, useful when a reply garbles |
| `/whoami` | ⏸️ deferred | Debugging ACL over mesh (needs ACL access-level plumbing) |
| `/usage` | ✅ done | Observability |
| `/version` | ✅ done | Trivial, good for `/help` |

**Session control — medium value:**
| Command | Status | Description |
|---------|--------|-------------|
| `/resume [name]` | ⬜ open | Resume a named session (complements `/new`) |
| `/branch [name]` (alias `/fork`) | ⬜ open | Branch the current session |
| `/compress` (alias `/compact`) | ⬜ open | Compress context; `--preview` to preview |
| `/undo [N]` | ⬜ open | Back up N user turns and re-prompt |
| `/title [name]` | ⬜ open | Title the current session |
| `/sessions` | ⬜ open | Browse/resume previous sessions |
| `/background` (alias `/bg`, `/btw`) | ⬜ open | Run a prompt in the background |
| `/queue` (alias `/q`) | ⬜ open | Queue a prompt without interrupting |
| `/steer` | ✅ done | Inject a message after next tool call |
| `/agents` (alias `/tasks`) | ⬜ open | Show active agents / running tasks |
| `/loop` (alias `/proactive`) | ⬜ open | Re-run a prompt on interval |
| `/goal` | ⬜ open | Standing goal across turns |
| `/heartbeat` (alias `/hb`) | ⬜ open | Recurring prompt when idle |
| `/restart` | ⬜ open | Gracefully restart the gateway after draining |
| `/save` | ⬜ open | Export conversation (json/md/html) |
| `/refine` | ⬜ open | Save lessons to memory/skills |
| `/moa` | ⬜ open | Mixture-of-Agents preset |
| `/subgoal` | ⬜ open | Manage extra criteria on active goal |
| `/sethome` (alias `/set-home`) | ⬜ open | Set chat as home channel |
| `/start` | ⬜ open | Acknowledge platform start pings |

**Config / display — lower priority for mesh (UI-leaning, but not cli_only):**
| Command | Description |
|---------|-------------|
| `/codex-runtime` | Toggle codex app-server runtime |
| `/personality [name]` | Set a predefined personality |
| `/voice [on|off|tts|status]` | Toggle voice mode *(voice over mesh = constrained, see Tier 3)* |
| `/yolo` | Toggle YOLO (skip approvals) |
| `/approvals [manual|smart|off]` | Persistent dangerous-command approval mode |
| `/reasoning` | Manage reasoning effort/display |
| `/fast [normal|fast|status]` | Fast mode toggle |
| `/footer [on|off|status]` | Gateway runtime-metadata footer |

**Tools & Skills (not cli_only):**
| Command | Description |
|---------|-------------|
| `/memory` | Review/approve pending memory writes |
| `/bundles` | List skill bundles |
| `/learn` | Learn a reusable skill from a description |
| `/init` | Generate AGENTS.md from a repo scan |
| `/suggestions` (alias `/suggest`) | Review suggested automations |
| `/blueprint` (alias `/bp`) | Set up an automation from a template |
| `/curator` | Background skill maintenance |
| `/kanban` | Multi-profile collaboration board |
| `/reload-mcp` / `/reload-skills` | Reload MCP servers / rescan skills |
| `/plugins` *(cli_only — N/A over mesh)* | — |

**Info / misc (not cli_only):**
| Command | Description |
|---------|-------------|
| `/diff` | Show git changes |
| `/platforms` (alias `/gateway`) *(cli_only — N/A)* | — |
| `/platform <pause|resume|list>` | Pause/resume a failing platform |
| `/update` | Update Hermes Agent |
| `/subscription` *(cli_only — N/A)* | — |
| `/topup` | Nous balance / billing |
| `/insights [days]` | Usage insights |
| `/debug` | Upload debug report |

> **Explicitly `cli_only` (do NOT port to mesh):** `/clear`, `/redraw`,
> `/history`, `/prompt` (compose), `/worktree`, `/snapshot`, `/export`,
> `/import`, `/journey`, `/config`, `/statusbar`, `/battery`, `/timestamps`,
> `/verbose`, `/focus`, `/skin`, `/indicator`, `/wake`, `/busy`, `/tools`,
> `/toolsets`, `/skills`, `/cron`, `/reload`, `/browser`, `/plugins`,
> `/platforms`, `/copy`, `/paste`, `/image`, `/subscription`, `/quit`.
> These depend on a terminal/TUI/clipboard and have no mesh equivalent.

> **Note:** `/stop` also appears in `cli_commands_mixin.py`; the registry above
> is the authoritative list for what a gateway platform exposes.

---

## Tier 3 — Interaction parity (non-command behaviors)

| Item | Telegram behavior | Mesh status | Notes |
|------|-------------------|-------------|-------|
| Streaming replies | Token-by-token | ⬜ | LXMF is message-based, no streaming. Out of scope unless we chunk. |
| Tool-call visibility | Renders tool activity | ⚠️ partial (2026-08-22) | Live `🔧 tool` push via `agent:step` hook → control server; `/approve`/`/deny` gate + `/steer`; state.db recap fallback. Caveat: `agent:step` fires *after* tool execution — veto, not pre-execution gate. See `mesh-bridge-findings-2026-08-22-tool-calls.md`. |
| Attachments (image/file) | User sends image/file, agent sees it | ⬜ in scope (Tier 5) | LXMF text-only for small messages, but RNS `Resource` can carry the bytes and LXMF the hash pointer — same pattern as voice-out (5.1). Inbound direction has no LXMF file-receive; needs the same `rngit`/spike path as voice-in (5.2). Added to roadmap 2026-09-16: both send-image-to-agent and send-image-back are now tracked under Tier 5 media. |
| Voice (STT/TTS) | Voice note in, voice out | ⬜ verify | Constrained over mesh; likely out of scope. |
| Markdown/formatting | Rich formatting | ⬜ | LXMF is plain text; keep replies plain-text-safe (TTS-friendly). |
| Multi-turn tool loops | Agent runs tools, replies | ⚠️ | Works (it's `hermes chat`), but the *rendering* of tool activity is the gap (see above). |

---

## Tier 4 — Ops / hardening (safe to leave on + safe to PR)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 4.1 | **Redaction before PR** | ✅ done for our commit | PII/model-names/hashes scrubbed; `model_command.py` no longer hardcodes the catalog. Upstream PR uses the clean branch. |
| 4.2 | **Config-driven, deployment-agnostic** | ✅ done | Models discovered from `config.yaml`; no deployment specifics in code. |
| 4.3 | **`/status` health endpoint** | ✅ done | `GET /status` on control server (commit `4ce5db8`): model + session + uptime + ACL mode. Bridge `/status` also shows `Bridge: up Xh Ym` + session-cumulative tool total (commit `cc232e8`). |
| 4.4 | **Structured logging of commands** | ⚠️ partial | Commands log to journal (`Command from <id>: /new`) but not to the session DB. Consider a lightweight audit log. |
| 4.5 | **Watchdog / auto-restart** | ✅ done | **Option 3b** (see `docs/mesh-bridge-findings-2026-09-09-watchdog-option-1-archived.md` for why Option 1 was shelved). `core/bridge_liveness.py` `BridgeLiveness` runs a daemon thread in the bridge that probes RNS liveness (`RNS.Transport.interface_last_jobs`, refreshed every 5s) and pings systemd's watchdog (`sd_notify(WATCHDOG=1)`) each tick; on a stale probe it stops pinging so systemd kills + `Restart=on-failure` restarts. Also writes an idle-heartbeat marker (`~/.hermes/.reticulum-idle-heartbeat`, mtime-authoritative) for `/status` + post-mortem. Service files flipped to `Type=notify` + `NotifyAccess=all` + `WatchdogSec=90` (same unit — no new service). `/status` shows `Watchdog: ✓ RNS alive (heartbeat Xs ago)` / `✗ RNS not responsive`. Env: `HERMES_BRIDGE_LIVENESS_INTERVAL` (30s), `HERMES_BRIDGE_RNS_PROBE_MAX_AGE` (30s), `HERMES_BRIDGE_HEARTBEAT_FILE`. 10 tests in `TestBridgeLiveness`; end-to-verified against a live RNS daemon (probe `False` pre-RNS → `True` post-RNS, pings flow). Catches: RNS-wedged-but-process-alive, and total process freeze. |
| 4.6 | **CI on the fork** | ⬜ open | Lint/import check before PR. Currently manual. |
| 4.7 | **Pre-execution approval gate** | ✅ done | `mesh-tool-gate` `pre_tool_call` plugin in repo (commit `f8b0c69`): risky tools POST to `/gate/notify` pre-execution; verdict rendered over LXMF. Session-name mismatch fixed (`6d5d0a1`); `/deny` aligned to Telegram block-and-continue (`63e753f`); timeout vs explicit deny distinguished (`c80645a`). Alias normalization fixed (`4a2fd06`). Three-timeout alignment documented (590/600). |

---

## Open questions to close before building

- Does `chat -q -c <name>` resume one thread, or mint a new session per call?
  (DB says per-call — needs a direct test. **This is Tier 1.1.**)
- Which Tier-2 commands do we actually want over mesh vs. keep Telegram-only?
  **DECIDED 2026-08-20:** fuller session control is in scope — land the
  high-value batch first, then the full non-`cli_only` session set
  (`/resume`, `/branch`, `/compress`, `/undo`, `/queue`, `/steer`, `/background`,
  `/sessions`, …).
- **Voice & media: in scope as Tier 5.** Voice-out (5.1) is feasible now.
  Voice-in (5.2) needs a spike on the `rngit` inbound path. **Live call (5.3)
  is out of scope** for the LXMF transport — documented, not deferred.
  Images joined the same tier 2026-09-16: image-out (5.4) rides the 5.1
  RNS-Resource plumbing; image-in (5.5) shares the 5.2 inbound-transport
  spike.
- Does `/stop` need to kill the `hermes chat` subprocess, or just flag the
  bridge to ignore the next reply?

## Build order (value × effort)

1. ~~**Tier 1** — fix continuity (1.1) + add the responsiveness guard (1.2).~~ ✅ done
2. ~~**Tier 2 high-value batch** — `/status`, `/stop`, `/pause`, `/approve`/`/deny`,
   `/retry`, `/whoami`, `/usage`, `/version`.~~ ✅ done (all except `/whoami` — deferred)
3. **Tier 3** — tool-call / clarify / accept-deny rendering over plain text. (Tool-call visibility landed via step-watcher; clarify/accept-deny rendering still open.)
4. **Tier 4** — CI (4.6). (4.3 health endpoint ✅, 4.5 watchdog ✅, 4.7 pre-exec gate ✅.)
5. **Tier 2 fuller session control** — the full non-`cli_only` session set
   (`/resume`, `/branch`, `/compress`, `/undo`, `/queue`, `/background`,
   `/sessions`, …) once the core batch is stable.
6. **Tier 5 — Voice & media** — voice-out first (5.1), then image-out (5.4)
   reusing the same RNS-Resource transfer code, then a shared spike on the
   inbound path for voice-in (5.2) and image-in (5.5). See the transport
   constraints below before committing to the inbound side.
7. Then cut the upstream PR from the clean branch (after Tier 4).

---

## Tier 5 — Voice & media over LXMF (transport-constrained)

**Verified transport facts** (RNS 1.4.2 + LXMF 1.1.1, read from the venv,
2026-08-20):

- **LXMF is a tiny-message protocol, not a streaming one.** Each message carries
  **~368 bytes of content max** (documented in `LXMessage.py`: ~112B fixed
  overhead, ~368B content). No chunked/streaming voice channel exists.
- **Outbound file transfer exists.** RNS `Resource.py` provides a real
  `Resource` class for transferring arbitrary bytes over an RNS Link
  (hashmap + chunked transfer). LXMF carries only the **hash/pointer**
  (~60 bytes — fits), the client pulls the actual file.
- **Inbound file transfer is the gap.** LXMF messages are content-only (no
  file-receive). The only inbound binary path in the stack is RNS
  **`rngit`** (`RNS/Utilities/rngit/{client,server}.py`) — a git-backed
  resource server. That is a heavier, separate subsystem.
- **Live two-way "call" is not realistic** over LoRa/LXMF: 368-byte messages,
  opportunistic delivery, no QoS. The honest model is **async voice messages**,
  not a phone call.

### Sub-tiers

| # | Item | Feasibility | Notes |
|---|------|-----------|-------|
| 5.1 | **Voice-out** (TTS → OGG/Opus → RNS-Resource → LXMF pointer) | ✅ medium, high feasibility | Generate OGG locally (edge-tts/piper already in the stack's toolchain), transfer via RNS `Resource`, LXMF message carries hash+size. Mirrors Telegram voice bubbles. |
| 5.2 | **Voice-in** (STT: your note → faster-whisper → text) | ⚠️ hard — spike first | No LXMF inbound file path. Options: (a) RNS `rngit` server the phone pushes to; (b) base64-chunked text (fragile, 368B/msg — only for tiny clips). **Spike 5.2 before building.** |
| 5.3 | **Live call** (continuous two-way voice) | ❌ out of scope | Not feasible on this transport. Documented, not deferred. |
| 5.4 | **Image-out** (agent sends image/file back to you) | ✅ medium, high feasibility | Same pattern as 5.1: save image locally, transfer bytes via RNS `Resource`, LXMF message carries hash+size (+ format hint). Client fetches over the RNS link. Feasible now once the 5.1 resource plumbing is in place; build after 5.1 shares the transfer code. |
| 5.5 | **Image-in** (you send image/file to the agent) | ⚠️ hard — spike first | No LXMF inbound file path, same gap as 5.2. Options: (a) RNS `rngit` server you push to; (b) base64-chunked text (fragile at 368B/msg — images are too big). Agent side is easy once bytes arrive: save to disk, hand path to `vision_analyze` / `read_file`. **Spike 5.5 together with 5.2 — they share the same inbound-transport question.** |

### Open design questions for Tier 5
- Voice-out codec + bitrate for LoRa bandwidth (Opus q~0.3? ~16–24 kbps?).
- Where the OGG is stored + how the client is told the resource hash (a
  structured LXMF message field, or a short text preamble).
- For 5.2: do we stand up `rngit`, or is voice-out-only an acceptable v1?
- For 5.4/5.5 (images): image size ceiling for LoRa hop counts (a phone photo
  is 2–5 MB; RNS Resource chunking handles it, but how slow at 3 hops?).
  Format hints in the LXMF pointer message (JPEG/PNG/webp) so the client
  renders without sniffing. And: does image-in need `rngit` like 5.2, or can
  a shared RNS Resource the phone pushes to (if the RNode client supports
  Resource upload) do the job — that decides whether 5.5 and 5.2 spike once
  or twice.
