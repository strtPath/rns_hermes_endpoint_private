# Finding: Tool calls are invisible on the Reticulum mesh (2026-08-22)

## Symptom
On the mesh, a turn that used tools arrived as the final answer only. No tool
previews, no live updates, no recap — the operator could not see that Hermes
was working, could not veto a dangerous tool mid-turn, and could not steer the
agent while it was running.

## Why (architecture, not a bug)
The bridge's `HermesClient` spawns `hermes chat -q … -Q` as a subprocess per
message. Two facts make tool events structurally invisible:

1. `-Q` (quiet) suppresses banner, spinner, and **tool previews** by design —
   the child's stdout carries only the final response.
2. The hermes agent core *does* have tool-event hooks, but the ones that exist
   in-process are the only ones wired anywhere: `agent.step_callback` (per
   iteration, with the previous round's tool names/args/results) and
   `agent.tool_progress_callback` (pre-execution preview). The gateway wires
   `step_callback` for its own tool-status UI; a plain CLI child wires
   neither, and the bridge has no event channel back from the child at all.

So the bridge process only ever sees: stderr startup banners, then the final
stdout. Nothing else.

## Design constraints (user-stated)
- **No changes to the hermes agent installation** (`~/.hermes/hermes-agent/`):
  it is constantly updated, and the project is intended to be shareable —
  altering the agent would alter every user's install.
- Everything must live in `rns_hermes_endpoint` plus user-local hermes config
  (hooks in `~/.hermes/hooks/`, which are *not* part of the agent install).
- Veto + gate semantics: `/deny` stops the current turn; `/approve` is a
  per-turn gate, deny-by-default for risky tools; non-risky tools always run.
- The operator wants tool calls to **arrive on the mesh as they happen**, so
  they can `/stop` or `/steer` before something bad ships.

## Solution (implemented in the fork)
Four pieces, all in `rns_hermes_endpoint` except the hook file:

### 1. Hook: `~/.hermes/hooks/mesh-tool-events/` (user-local, ships with the project)
`HOOK.yaml` subscribes to the `agent:step` event; `handler.py`:
- resolves the mesh session name by querying `state.db` for the session whose
  `id` matches the event's `session_id` and returning its `title` (the bridge
  creates child sessions with `-c <title>`, so `title` == mesh thread name);
- classifies each reported tool as safe / risky (unknown → reported, not gated);
- POSTs each step to the bridge's control endpoint:
  - `kind: "report"` (safe tools) — non-blocking, 10s timeout, recap only;
  - `kind: "gate"` (risky tools) — **blocking** (up to 600s) until the
    operator answers `/approve` or `/deny` on the mesh; timeout → deny.

The hook is deliberately passive: it never raises into the conversation loop;
any failure is logged and swallowed so a bridge outage can't break a normal
hermes turn.

### 2. `core/control_server.py` (new)
Local HTTP endpoint, `127.0.0.1:8471` only, token-authenticated (token
generated at startup, written 0600 to `~/.lxmf/storage/control_token`; the
hook reads it from the same file).
- `POST /step` — receives hook events, records `ToolStep`s per session and per
  turn; for `kind: "gate"` it blocks in `request_approval()` until the
  operator decides, then fires callbacks:
  - `on_gate_open(session, tool)` → push "⏸️ waiting for /approve" to mesh;
  - `on_deny(session)` → the CLI wires this to `hermes.stop()`, i.e. the veto
    actually kills the in-flight child;
  - `on_step(session, step)` → push a live "🔧 <tool>" message to the mesh peer.
- `POST /approve` / `POST /deny` / `POST /steer` — also reachable over HTTP
  (external drivers), though mesh commands call the same methods in-process.
- State: per-session pending gate (`threading.Event`), decision, per-turn step
  list (for `/tools`), steering text queue (consumed by `HermesClient.chat`).

### 3. `core/commands.py` — new mesh commands
- `/approve` (alias `a`) — release the pending gate.
- `/deny` (alias `d`) — deny the gate; the control server then fires
  `on_deny` → `hermes.stop()`, aborting the turn.
- `/steer <text>` — queue steering text; `HermesClient.chat()` prefixes the
  next prompt with it (consumed once).
- `/tools` (alias `t`) — recap of tool steps this turn (last 10).
- `/verbose on|off` — toggle detailed tool recaps.
- `/status` now shows pending-approval state and step count.

### 4. `core/hermes_client.py` — recap fallback + steer consumption
- `tool_recap(limit)` reads the child session's persisted assistant
  `tool_calls` from `state.db` (read-only) and returns the last N tool names.
- `_with_tool_recap(reply)` appends `🔧 name1, name2, …` to every successful
  reply. This is the **safety net**: even if the hook stops firing (hermes
  update renames the event, hook disabled, etc.) the mesh still sees which
  tools ran.
- `steer(text)` / `pop_steer()` — consumed at the top of `chat()`, prefixed to
  the prompt as an operator instruction.

### 5. `cli.py` — wiring
- Creates and starts the `ControlServer` (before the bridge).
- `ctrl.on_deny = hermes.stop()` (veto path).
- `ctrl.on_step` → `bridge.send_reply(peer_hash, "🔧 <tool>", peer_ident)` —
  the live push. The peer is resolved by tracking the sender of the message
  that started the turn (`mesh_push[session] = {hash, ident}`).
- Passes the control server into `build_dispatcher(..., control_server)`.

## Honest limitation (read before trusting the gate)
`agent:step` fires **after** each iteration's tools have executed (it reports
`prev_tools`). So the semantics delivered are:

- **Live visibility**: every tool call arrives on the mesh as it completes
  (`🔧 terminal`, `🔧 web_search`, …) — including errors (`❌`).
- **Veto**: `/deny` (or `/stop`) kills the in-flight hermes child immediately.
- **Deny-by-default acknowledgment gate** for risky tools: the hook blocks
  until the operator acknowledges or the 120s default timeout (configurable
  via `HERMES_MESH_APPROVAL_TIMEOUT`) elapses — on timeout the tool is denied
  and the turn is aborted.

It is **not** a pre-execution gate: the risky tool runs once, then the operator
gets the veto. True pre-execution blocking would need the agent core's
`tool_progress_callback` (in-process, gateway-side) — which is out of scope by
the no-agent-changes constraint.

Recommended follow-up (not yet implemented): a **deny-list** in the hook for
tools that should never run on the mesh at all (`terminal`, `write_file`,
`patch`, `cronjob`, …) — the hook treats them as instantly denied (immediate
`on_deny`, no wait), rather than gate-waiting. The gate then stays for tools
that are *sometimes* OK (e.g. `web_search` on an unusual domain, `execute_code`
for long jobs).

## Files changed (fork)
- `src/hermes_reticulum/core/control_server.py` — new (HTTP endpoint, gate,
  state, callbacks).
- `src/hermes_reticulum/core/commands.py` — `/approve`, `/deny`, `/steer`,
  `/tools`, `/verbose`; `/status` extended; `CommandContext.control_server`;
  `build_dispatcher(..., control_server)`.
- `src/hermes_reticulum/core/hermes_client.py` — `steer()` / `pop_steer()` /
  `tool_recap()` / `_with_tool_recap()`; steering consumed in `chat()`.
- `src/hermes_reticulum/cli.py` — control server lifecycle + callbacks; peer
  tracking for live pushes.
- `~/.hermes/hooks/mesh-tool-events/HOOK.yaml` + `handler.py` — user-local
  hook (ships with the project; install = copy into `~/.hermes/hooks/`).

Env vars (all optional):
- `HERMES_MESH_CONTROL_URL` — default `http://127.0.0.1:8471`.
- `HERMES_MESH_TOKEN_FILE` — default `~/.lxmf/storage/control_token`.
- `HERMES_MESH_PROFILE` — only stream steps for sessions whose mesh title
  starts with this prefix (multi-bridge setups).
- `HERMES_STATE_DB` — default `~/.hermes/state.db` (hook + client).

## Verification
- All modified files `py_compile` clean.
- Existing 7 liveness-guard unit tests still pass
  (`./venv/bin/python -m unittest tests.test_hermes_client -v`).
- Not yet run: service restart + live mesh turn (needs user approval for
  `systemctl --user restart hermes-reticulum`).

## Status
- [x] Control server + token auth
- [x] Hook (agent:step) with risk classification + blocking gate
- [x] `/approve` `/deny` `/steer` `/tools` `/verbose` commands
- [x] Recap fallback from state.db
- [x] Live `🔧 tool` push to mesh peer
- [x] Veto path (`/deny` → `hermes.stop()`)
- [ ] Unit tests for gate/steer/recap (next)
- [ ] Findings doc (this file)
- [ ] Live mesh verification after service restart
- [ ] Optional: hook deny-list for always-forbidden tools
- [ ] Update `docs/feature-parity-roadmap.md` Tier 3 status
