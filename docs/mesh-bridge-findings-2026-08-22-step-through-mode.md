# Mesh Bridge — Step-Through Mode (2026-08-22)

## Goal

"Print the full tool call and full output to the user before the model
moves on to the next action, delivered as multiple LXMF posts (no
streaming over Reticulum)."

## What landed

| Component | File | Change |
|-----------|------|--------|
| `StepThroughManager` | `core/bridge.py` | Mode flag + hold gate (checkpoint). Writes state files the hook polls. |
| `LXMFBridge.push_reply()` | `core/bridge.py` | Chunked multi-post LXMF send (≤1500 chars/part, `[n/N]` numbered). |
| `HermesClient.set_step_mode()` | `core/hermes_client.py` | Toggles step-through; injects a prompt prefix instructing the model to announce each tool before calling it. |
| `HermesClient._apply_hold_gate()` | `core/hermes_client.py` | Blocks the final reply until `/go` (or 30-min timeout) when `/hold` was pressed. |
| `HermesClient.set_push_callback()` | `core/hermes_client.py` | Lets the bridge push proactive messages (hold notifications) to the mesh peer. |
| `ControlServer.on_full_step` | `core/control_server.py` | New `/step/full` endpoint: the hook POSTs the full tool I/O text here; the bridge chunks and pushes. |
| `_on_full_step` | `cli.py` | Wires `/step/full` → `bridge.push_reply()` (chunked multi-post). |
| `/steps on\|off`, `/hold`, `/go` | `core/commands.py` | Mesh commands to toggle step-through and the checkpoint gate. |
| Hook `agent:step` handler | `~/.hermes/hooks/mesh-tool-events/handler.py` | In step-through mode, POSTs the full tool call (name + args) and full tool output to `/step/full`. |

## Architecture

```
Gateway process (hermes chat child)
  └─ agent:step hook fires AFTER each tool batch
       │
       │  POST /step/full {session, body: "🔧 tool\n── args ──\n...\n── output ──\n..."}
       ▼
ControlServer (127.0.0.1:8471, in bridge process)
  └─ on_full_step(session, text)
       │
       │  bridge.push_reply(peer_hash, text)
       ▼
LXMFBridge.push_reply()
  └─ split_message(text, 1500) → ["[1/3] ...", "[2/3] ...", "[3/3] ..."]
       │
       │  send_reply() × N (one LXMF message per chunk, 500ms gap)
       ▼
Mesh peer (user's RNode / Sideband)
```

## What the user sees (step-through ON)

1. **Before the model acts**: the model's own text announcing the tool
   (injected by the step-through prompt prefix) arrives as the normal
   reply path.
2. **After each tool runs**: the hook fires, the bridge pushes the full
   tool call arguments and full tool output as chunked `[n/N]` LXMF posts.
3. **Checkpoint gate** (opt-in via `/hold`): the final reply is held
   until the user sends `/go` (or auto-releases after 30 minutes).

## Honest limitation

`agent:step` fires *after* the tool batch has already executed. There is
no pre-execution gate for safe tools. The "show before next action"
guarantee is:
- The model is instructed to state which tool it's about to run (prompt
  prefix), so the user sees the intent *before* the tool runs.
- The full tool I/O is delivered immediately *after* the tool runs and
  *before* the model's next step (the hook fires between iterations).
- Risky tools still go through the `/approve`/`/deny` gate (veto).

This is the closest the mesh transport allows without streaming.

## State files (bridge ↔ hook, cross-process)

| File | Written by | Read by | Content |
|------|-----------|---------|---------|
| `~/.hermes/.reticulum-step-mode` | Bridge (`/steps`) | Hook (each `agent:step`) | `1` = step-through ON |
| `~/.hermes/.reticulum-hold-state` | Bridge (`/hold`/`/go`) | Hook (each `agent:step`) | `1` = hold active |

## Bandwidth cost

A 32 KB tool output ≈ 21 chunked posts of 1500 chars. The user
explicitly accepted this cost.

## TODO / future

- The hold gate in `hermes_client.py` uses a simple `time.sleep(1)` poll
  loop. Could be replaced with a `threading.Event` for cleaner shutdown.
- `StepThroughManager.gate_and_wait()` in `bridge.py` is defined but
  currently the hold gate lives in `hermes_client.py` instead (simpler
  wiring). Consolidate if needed.
- The `/steps` toggle writes the mode file; the hook polls it on every
  `agent:step` event (no caching), so toggles take effect immediately
  on the next tool batch.
