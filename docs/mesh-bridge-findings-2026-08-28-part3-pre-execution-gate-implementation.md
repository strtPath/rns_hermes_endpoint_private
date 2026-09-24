# Mesh Bridge — Pre-Execution Gate: Implementation (Part 3)

*2026-08-28. Companion to `part2-pre-execution-gate.md` (feasibility). This is
the build that landed.*

## What was built

A **pre-execution approve/deny gate** for the Reticulum/LXMF mesh bridge,
replacing the old *acknowledge-then-veto* model. The operator now sees risky
tool calls **before** they run and can approve/deny — the same kinds of risky
actions Hermes already gates by default, because the gate reuses Hermes' own
detection, not a re-implementation.

### 1. `pre_tool_call` plugin — `~/.hermes/plugins/mesh-tool-gate/`

- Fires **in-process, before dispatch** (in `agent/tool_executor.py::_authorized_dispatch`), so
  the tool has not run yet. This is the correct pre-execution layer.
- **Reuses Hermes' own detection** — `detect_dangerous_command` and
  `detect_hardline_command` from `tools/approval.py` — the exact functions the
  terminal tool gates on. Zero drift: whatever Hermes blocks/approves by
  default, the mesh gate blocks/approves identically.
- Directive contract (`hermes_cli/plugins.py`):
  - `{"action":"block","message":…}` — hardline + already-gated commands: the
    tool never runs; the message is what the model sees.
  - `{"action":"approve","message":…,"rule_key":…}` — dangerous commands:
    escalates to a human approve/deny, fail-closed on timeout.
  - `None` — safe tools (and non-terminal tools): proceed; output still streams
    via the existing `agent:step` hook.
- **Fail-closed by construction**: the `approve` directive resolves through
  `authorization_gate`; on error/deny/timeout it becomes a block.

### 2. Control-server route — `core/control_server.py::/gate/notify`

- New POST route: opens an approval gate for `(session, tool, command,
  description)`, fires the (new) `on_gate_open` callback, and **blocks the
  request thread** up to `approval_timeout` (default 60s) waiting for `/approve`
  or `/deny` (existing routes, unchanged).
- Returns the verdict in the HTTP response; the plugin maps it:
  - `approve` → tool runs
  - `deny` / `timeout` → **block** (fail-closed)
  - no session → 400, bad token → 401
- `on_gate_open` normalized to a 4-arg signature
  `(session, tool, command, description)` so the `/step` ack gate and the
  `/gate/notify` pre-exec gate share one push callback.

### 3. Mesh push — `cli.py`

- Wired `ctrl.on_gate_open` → `bridge.send_reply(…, "⏳ pending pre-execution
  approval: {tool}\n{command}\nSend /approve or /deny")`. Reuses the existing
  `mesh_push` map (same session→peer routing as `on_step`), bounded via the
  bridge's `relay()` (worker thread, 60s cap) so the request thread — and
  transitively the gateway event loop — is never parked unbounded.

### 4. `mesh-tool-events` hook — `~/.hermes/hooks/mesh-tool-events/handler.py`

- **Neutered the two blocking `/step` `kind=gate` paths** (step-through mode +
  normal mode, each a 600s blocking wait). The gate moved out of this hook:
  the plugin now owns approve/deny pre-dispatch. The hook is now **pure
  stream**: liveness marker + step-through full step + recap `/step` report.
  No more double-gating the operator, no more 600s worker park.

## Why the gateway event loop is not parked (the skill's #1 warning)

- The plugin's `on_pre_tool_call` runs synchronously in the loop, but only for
  **terminal** commands and only when `detect_dangerous_command` fires. It makes
  **one bounded blocking POST** (`HERMES_MESH_GATE_TIMEOUT`, default 60s) to the
  control server.
- The control server does the (possibly slow) LXMF relay **off its own request
  thread** (via `relay()`), blocking only the request thread — which is the
  plugin's HTTP client thread, not the gateway's event loop.
- Net effect: the gateway loop is parked **at most 60s, and only for a
  dangerous terminal command**. Safe tools cost ~0ms (one local detect call,
  no network). This is bounded and acceptable; the old 600s unbounded park is gone.

## Verification (all real runs, this session)

- Plugin compiles; loads via `PluginManager.discover_and_load()`; registers
  `pre_tool_call → on_pre_tool_call` (and it is in `plugins.enabled`).
- Detection reuse confirmed against `tools/approval.py`:
  - `rm -rf /` → hardline (block, no gate)
  - `rm -rf /tmp` → dangerous (gate)
  - `curl | sh` → dangerous (gate)
  - `ls` / read_file / web_search → None (proceed)
- Control-server integration (scratch port, real token):
  - `/gate/notify` + `/approve` → `approve`
  - `/gate/notify` + `/deny` → `deny`
  - `/gate/notify` no answer → `deny` in 3.0s (fail-closed)
  - bad token → 401; no session → 400
- `control_server.py`, `cli.py`, plugin `__init__.py`, and the edited hook all
  `py_compile` clean.

## Deploy note (do when the operator is NOT AFK / the turn is idle)

The **running bridge (pid 8471) still has the old ControlServer** (no
`/gate/notify` route). Until it is restarted, the plugin's `/gate/notify` POST
404s and the gate **fail-closes (blocks)** — i.e. dangerous terminal commands
on the mesh are blocked until the bridge is restarted with the new code. Safe
tools are unaffected. **Restart the bridge (and the gateway, to load the
plugin) before relying on this gate.** The plugin + control-server code is
committed and ready; the running process just predates it.

## Files
- `~/.hermes/plugins/mesh-tool-gate/{plugin.yaml, __init__.py}` (new; enabled)
- `src/hermes_reticulum/core/control_server.py` (`/gate/notify`, `on_gate_open` 4-arg)
- `src/hermes_reticulum/cli.py` (wired `ctrl.on_gate_open` push)
- `~/.hermes/hooks/mesh-tool-events/handler.py` (gate paths removed; pure stream)
- `plugins.enabled += mesh-tool-gate` in `~/.hermes/config.yaml`
