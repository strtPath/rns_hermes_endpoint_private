# Mesh Bridge — Feature Parity Roadmap (vs Telegram gateway)

_Goal: bring the Reticulum/LXMF mesh bridge (`rns_hermes_endpoint`) to feature
parity with the Hermes **Telegram** gateway, so the mesh endpoint is a first-class
way to talk to the agent, not a degraded one. This is a living doc — update the
Status column as we land items._

_Last updated: 2026-08-20. Owner: Holo + user._

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
| 1.5 | **Startup loud-fail on missing `hermes`** | ⚠️ partial | `find_hermes_bin()` raises `RuntimeError`; service unit now pins `PATH`. Add a clear, actionable startup message + a `/status`-style check. |

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

### Not `cli_only` — in parity scope (grouped, with mesh priority)

**High value — do next (cheap + closes real pain):**
| Command | Description | Why it matters for mesh |
|---------|-------------|-------------------------|
| `/status` | Session, model, token, context info | Self-diagnosis over mesh (also Tier 4) |
| `/stop` | Kill running background processes | Escapes a hung 27b run (pairs with 1.2) |
| `/pause` | Global emergency stop (`/pause off` resumes) | Mesh-specific safety valve |
| `/approve` / `/deny` | Approve/deny pending dangerous commands | The gateway's approval model over mesh |
| `/retry` | Resend last message | Cheap, useful when a reply garbles |
| `/whoami` | Show slash-command access level | Debugging ACL over mesh |
| `/usage` | Token usage / rate limits | Observability |
| `/version` | Hermes version | Trivial, good for `/help` |

**Session control — medium value:**
| Command | Description |
|---------|-------------|
| `/resume [name]` | Resume a named session (complements `/new`) |
| `/branch [name]` (alias `/fork`) | Branch the current session |
| `/compress` (alias `/compact`) | Compress context; `--preview` to preview |
| `/undo [N]` | Back up N user turns and re-prompt |
| `/title [name]` | Title the current session |
| `/sessions` | Browse/resume previous sessions |
| `/background` (alias `/bg`, `/btw`) | Run a prompt in the background |
| `/queue` (alias `/q`) | Queue a prompt without interrupting |
| `/steer` | Inject a message after next tool call |
| `/agents` (alias `/tasks`) | Show active agents / running tasks |
| `/loop` (alias `/proactive`) | Re-run a prompt on interval |
| `/goal` | Standing goal across turns |
| `/heartbeat` (alias `/hb`) | Recurring prompt when idle |
| `/restart` | Gracefully restart the gateway after draining |
| `/save` | Export conversation (json/md/html) |
| `/refine` | Save lessons to memory/skills |
| `/moa` | Mixture-of-Agents preset |
| `/subgoal` | Manage extra criteria on active goal |
| `/sethome` (alias `/set-home`) | Set chat as home channel |
| `/start` | Acknowledge platform start pings |

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
| Tool-call / clarify rendering | Renders tool activity, confirmations | ❌ open | Known upstream issue (see `reticulum-bridge-findings.md`): `unknown-toolsets` warning prefix + clarify/accept-deny prompts don't render over plain-text LXMF. |
| Attachments (image/file) | User sends image/file, agent sees it | ⬜ verify | LXMF text-only; likely out of scope. Confirm. |
| Voice (STT/TTS) | Voice note in, voice out | ⬜ verify | Constrained over mesh; likely out of scope. |
| Markdown/formatting | Rich formatting | ⬜ | LXMF is plain text; keep replies plain-text-safe (TTS-friendly). |
| Multi-turn tool loops | Agent runs tools, replies | ⚠️ | Works (it's `hermes chat`), but the *rendering* of tool activity is the gap (see above). |

---

## Tier 4 — Ops / hardening (safe to leave on + safe to PR)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 4.1 | **Redaction before PR** | ✅ done for our commit | PII/model-names/hashes scrubbed; `model_command.py` no longer hardcodes the catalog. Upstream PR uses the clean branch. |
| 4.2 | **Config-driven, deployment-agnostic** | ✅ done | Models discovered from `config.yaml`; no deployment specifics in code. |
| 4.3 | **`/status` health endpoint** | ⬜ add | Model + session + uptime + ACL mode — so we can self-diagnose over the mesh. |
| 4.4 | **Structured logging of commands** | ⚠️ partial | Commands log to journal (`Command from <id>: /new`) but not to the session DB. Consider a lightweight audit log. |
| 4.5 | **Watchdog / auto-restart** | ⬜ | systemd `Restart=` + a liveness ping so a dead bridge is caught. (Matches the CamoFox health-check pattern.) |
| 4.6 | **CI on the fork** | ⬜ | A lint/import check on the private fork before PR. |

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
- Does `/stop` need to kill the `hermes chat` subprocess, or just flag the
  bridge to ignore the next reply?

## Build order (value × effort)

1. **Tier 1** — fix continuity (1.1) + add the responsiveness guard (1.2).
2. **Tier 2 high-value batch** — `/status`, `/stop`, `/pause`, `/approve`/`/deny`,
   `/retry`, `/whoami`, `/usage`, `/version`. All are thin handlers on the
   existing `CommandDispatcher`.
3. **Tier 3** — tool-call / clarify / accept-deny rendering over plain text.
4. **Tier 4** — `/status` health, watchdog, CI.
5. **Tier 2 fuller session control** — the full non-`cli_only` session set
   (`/resume`, `/branch`, `/compress`, `/undo`, `/queue`, `/steer`, `/background`,
   `/sessions`, …) once the core batch is stable.
6. **Tier 5 — Voice & media** — voice-out first (5.1), then a spike on voice-in
   (5.2). See the transport constraints below before committing to 5.2.
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

### Open design questions for Tier 5
- Voice-out codec + bitrate for LoRa bandwidth (Opus q~0.3? ~16–24 kbps?).
- Where the OGG is stored + how the client is told the resource hash (a
  structured LXMF message field, or a short text preamble).
- For 5.2: do we stand up `rngit`, or is voice-out-only an acceptable v1?
