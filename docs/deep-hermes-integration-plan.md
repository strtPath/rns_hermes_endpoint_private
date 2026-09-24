# Deep Hermes Integration — Branch Plan

## Goal

Replace the subprocess-based `hermes chat -q` integration with in-process
`AIAgent` integration. The bridge imports Hermes' Python API directly and
drives the agent loop in the same process, gaining:

- Native `step_callback(api_call_count, prev_tools)` per tool batch
- `tool_start_callback(tool_name, args_preview)` / `tool_complete_callback(...)`
  for per-tool granularity
- Direct access to session state via `SessionDB` (no state.db polling)
- Native tool gating via `set_approval_callback` (per-thread) +
  `register_gateway_notify` / `resolve_gateway_approval` (per-session)
- Lower latency (no process spawn, no stdout pipe parsing, no HTTP relay)
- Single process (no gateway hook, no mesh-tool-gate plugin needed)

## Current architecture (baseline)

```
mesh client → LXMFBridge → HermesClient.chat()
                        → subprocess: hermes chat -q <msg>
                        → stdout pipe → reply text
                        → (separately) gateway hook → HTTP POST /step
                        → ControlServer → relay → mesh push
                        → (separately) mesh-tool-gate plugin → HTTP POST /gate/notify
                        → ControlServer → request_approval → approve/deny
```

## Target architecture

```
mesh client → LXMFBridge → HermesClient.chat()
                        → AIAgent (in-process, one per mesh session)
                        → run_conversation(user_message, ...)
                        → step_callback(api_call_count, prev_tools)
                          → directly pushes step to mesh (no HTTP hop)
                        → set_approval_callback + register_gateway_notify
                          → directly blocks for approve/deny (no HTTP hop)
                        → returns {"final_response": "...", ...}
                        → reply text → LXMFBridge → mesh
```

## Key API surface (verified from Hermes v0.21.0 source)

### Agent construction

```python
# Must run inside Hermes' venv (or with Hermes' site-packages on sys.path).
# The CLI does this via its own venv; the bridge needs the same.
from run_agent import AIAgent

agent = AIAgent(
    model="qwen3.8-27b@iq3_xxs",       # or omit for default from config
    session_id="mesh-reticulum-1234",  # resume by session (same as --resume)
    platform="cli",                     # affects system prompt formatting
    session_db=SessionDB(),            # SQLite session store
    # Tool callbacks (all optional, all per-thread via thread-local)
    step_callback=on_step,             # (api_call_count, prev_tools: list[dict])
    tool_progress_callback=on_tool_progress,  # (tool_name, args_preview)
    tool_start_callback=on_tool_start,        # (tool_name, args_preview)
    tool_complete_callback=on_tool_complete,  # (tool_name, args_preview, result)
    stream_delta_callback=on_delta,    # (text_delta) for live streaming
    event_callback=on_event,           # (event_name, data_dict)
    # ... many more available (thinking, reasoning, clarify, status, etc.)
)
```

### Running a turn

```python
result = agent.run_conversation(
    user_message="hello",
    conversation_history=None,  # agent tracks internally via session_db
    stream_callback=None,      # or pass for TTS-style streaming
    task_id="mesh-turn-1",     # isolates VMs between concurrent tasks
)
# result = {"final_response": "...", "messages": [...], ...}
```

### Tool gating (approval)

Two mechanisms, both per-session:

```python
# 1. Per-thread approval callback (set in the agent thread before run_conversation)
from tools.terminal_tool import set_approval_callback
set_approval_callback(my_callback)
# my_callback signature: (command, description) -> str choice
# ("once", "session", "always", "deny")

# 2. Per-session gateway notify (for blocking approvals)
from tools.approval import (
    register_gateway_notify,   # (session_key, cb) where cb(approval_data: dict)
    resolve_gateway_approval,  # (session_key, choice, resolve_all, reason, request_id)
    list_gateway_approvals,   # (session_key) -> list[dict]
    has_blocking_approval,    # (session_key) -> bool
    get_pending_gateway_approval,  # (session_key) -> dict | None
)
```

The approval flow:
1. Agent hits a dangerous command → calls `register_gateway_notify` callback
2. Callback pushes "⚠️ PRE-EXEC APPROVAL" to mesh
3. Mesh user responds → bridge calls `resolve_gateway_approval(session_key, "once"/"deny")`
4. Agent thread unblocks, tool proceeds or is denied

### Session management

```python
from hermes_state import SessionDB

db = SessionDB()
# Resume: db.get_messages_as_conversation(session_id) -> list[dict]
# Reopen: db.reopen_session(session_id)
# List: db.get_session(session_id) -> dict | None
```

## Callback signatures (verified)

| Callback | Signature | Fires when |
|----------|-----------|------------|
| `step_callback` | `(api_call_count: int, prev_tools: list[dict])` | After each tool batch, before next API call |
| `tool_progress_callback` | `(tool_name: str, args_preview: str)` | When a tool starts executing |
| `tool_start_callback` | `(tool_name: str, args_preview: str)` | Before tool execution (inline diffs) |
| `tool_complete_callback` | `(tool_name: str, args_preview: str, result: str)` | After tool completes |
| `stream_delta_callback` | `(text_delta: str)` | For each streaming text chunk |
| `event_callback` | `(event_name: str, data: dict)` | Structured events |
| `status_callback` | `(status: str)` | Agent status changes |

`prev_tools` in step_callback is a list of:
```python
[
    {
        "name": "terminal",
        "arguments": "{\"command\": \"ls\"}",
        "result": "file1\nfile2\n"
    },
    ...
]
```

## Phases

### Phase 1: Spike — prove the import works
- Add a `spike_inprocess.py` script that:
  - Sets sys.path to Hermes' venv site-packages
  - Imports `run_agent.AIAgent`, `hermes_state.SessionDB`
  - Constructs an agent with a trivial session_id
  - Calls `run_conversation("say hi")`
  - Prints the result
- Verify the agent has access to the same config, tools, and session state
- Verify step_callback fires and delivers tool info

### Phase 2: In-process chat() — replace the subprocess
- New `InProcessHermesClient` class (or mode flag on `HermesClient`)
- Construct `AIAgent` once per mesh session, reuse across turns
- `chat()` calls `run_conversation` directly, returns `final_response`
- Map existing features:
  - steering → drain `/steer` queue and prepend to user_message
  - step-through mode → step_callback pushes to mesh
  - hold gate → block after run_conversation returns (same as today)
  - liveness guard → no longer needed (use `run_budget_seconds` on AIAgent)
  - model pinning → AIAgent(model=...)
  - /stop → need to find Hermes' interrupt API (see risks)

### Phase 3: Native tool gating
- Before `run_conversation`, in the agent thread:
  - `set_approval_callback(mesh_approval_callback)`
  - `register_gateway_notify(session_key, mesh_notify_callback)`
- `mesh_approval_callback`: pushes to mesh, blocks, returns choice
- `mesh_notify_callback`: pushes "⚠️ PRE-EXEC APPROVAL" to mesh
- When mesh user responds: `resolve_gateway_approval(session_key, choice)`
- On session end: `unregister_gateway_notify(session_key)`

### Phase 4: Drop the HTTP relay path
- Remove ControlServer (or keep as fallback for subprocess mode)
- Remove mesh-tool-gate plugin dependency
- Remove gateway hook dependency
- Keep state.db reads for /tools recap and token stats (still useful)

### Phase 5: Feature parity + tests
- Verify all existing slash commands work (/model, /new, /stop, /steer, /hold, /go, /status)
- Verify step-through mode delivers identical output
- Verify deny-loop fix still holds (veto semantics)
- Run full test suite
- Update README + QUICKSTART

## Risks / open questions

1. **AIAgent constructor needs config** — the CLI does extensive setup before
   constructing AIAgent (credential loading, provider config, toolset
   registration, MCP discovery, session DB wiring, MCP startup).
   - The minimal path: `AIAgent(model=..., session_id=..., session_db=SessionDB())`
     with callbacks. The agent picks up its own config from `~/.hermes/`.
   - MCP discovery: `ensure_mcp_discovery_before_agent_build()` from
     `hermes_cli.mcp_startup` — need to call this before agent construction.
   - Credential loading: `self._ensure_runtime_credentials()` in the CLI —
     the agent reads `~/.hermes/.env` directly, so this may be automatic.

2. **Thread safety** — the bridge runs handlers in a ThreadPoolExecutor.
   AIAgent callbacks are per-thread (thread-local storage).
   - Option A: One agent per thread (matches CLI's ACP pattern).
   - Option B: Single agent with a lock (like the current `_turn_lock`).
   - The approval callback is per-thread, so Option A is cleaner.

3. **Session resume** — AIAgent takes `session_id` + `session_db`.
   The CLI's `_init_agent` loads `conversation_history` from the DB
   when `self._resumed` is True. We need to replicate this:
   - Load `db.get_messages_as_conversation(session_id)` before first turn
   - Pass as `conversation_history` to `run_conversation`
   - Subsequent turns: agent tracks internally (messages appended to
     `conversation_history` list)

4. **Interrupt/stop** — the current /stop kills the subprocess.
   In-process, we need Hermes' interrupt mechanism.
   - `cli.py` imports `request_hard_interrupt` — need to find its signature
     and what it does (likely sets a flag the conversation loop checks).
   - Alternative: `run_budget_seconds` on AIAgent for a hard timeout.

5. **venv compatibility** — the bridge venv and the Hermes venv may differ.
   - Option A: Run the bridge inside Hermes' venv (simplest).
   - Option B: `sys.path.insert(0, hermes_site_packages)` at startup.
   - Option C: Install hermes-agent as a dependency in the bridge's venv.
   - Option A is cleanest — the bridge already needs Hermes' full environment.

6. **Platform string** — AIAgent takes `platform="cli"`. For the mesh
   bridge, we should use a custom platform string like `platform="mesh"`
   so the system prompt doesn't inject CLI-specific hints.

## Files to change

- `src/hermes_reticulum/core/hermes_client.py` — add InProcessHermesClient
- `src/hermes_reticulum/cli.py` — wire in-process client, drop HTTP relay
- `src/hermes_reticulum/core/control_server.py` — mark optional (subprocess fallback)
- `pyproject.toml` — document Hermes venv requirement
- `spike_inprocess.py` — Phase 1 spike script
- `tests/test_inprocess.py` — in-process mode tests

## Out of scope

- Multi-model routing (the CLI's `_resolve_turn_agent_config` logic)
- Voice/TTS integration
- Image/multimodal input
- The full gateway plugin path (keep it working as-is for backward compat)
- The TUI/gateway server (the bridge is a headless mesh client, not a TUI)
