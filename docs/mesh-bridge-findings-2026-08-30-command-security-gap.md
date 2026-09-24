# Tool Command Security — Bridge vs Hermes Discrepancy Analysis

**Date:** 2026-08-30
**Source:** User report — "command security doesn't work like the hermes agent one does"
**Branch:** `feat/slash-commands-and-model-pin`

---

## The problem

The `mesh-tool-gate` plugin claims it "reuses Hermes' own single source of truth in `tools/approval.py`" — but what it actually does differs in several critical ways from how Hermes itself gates dangerous commands on Telegram/Discord.

---

## How Hermes gates dangerous commands (Telegram/Discord)

The path is:

1. `terminal` tool is about to execute → `agent/tool_executor.py::_authorized_dispatch`
2. Calls `tools/approval.py::detect_dangerous_command(command)` — 47+ patterns covering rm -rf, chmod 777, curl|sh, dd, mkfs, SQL DROP, find -exec rm, hermes gateway restart, docker lifecycle, self-termination, encoded payloads, Windows destructive, etc.
3. If dangerous → calls `check_dangerous_command()` which reads the session/permanent allowlist FIRST.
4. If not allowlisted → calls `prompt_dangerous_approval()` (CLI) or pushes to the gateway queue via `submit_pending()` + `register_gateway_notify()` (gateway).
5. The gateway renders the `[o]nce / [s]ession / [a]lways / [d]eny` prompt on Telegram/Discord.
6. User answers → `resolve_gateway_approval(session_key, choice)` → unblocks the agent thread.
7. **Hardline floor** (`detect_hardline_command`) runs FIRST, below YOLO — unconditionally blocks no-recovery commands (rm -rf /, mkfs, dd to raw device, fork bomb, kill -1, shutdown/reboot).

Key properties:
- **Session allowlist**: `[s]ession` persists for the life of the gateway session.
- **Permanent allowlist**: `[a]lways` persists in `config.yaml`.
- **Rule key grain**: each pattern has a stable `pattern_key` used for allowlisting.
- **Fail-closed by construction**: no human → denied. Timeout → denied.
- **YOLO mode**: user explicitly opts into "I accept the risk" — skips all dangerous checks (except hardline).
- **Smart approval**: an auxiliary LLM can auto-approve low-risk commands before the human sees them.
- **Cron/single-query mode**: configurable (`approvals.cron_mode: approve`).

---

## How the mesh-tool-gate plugin gates dangerous commands

The plugin's `on_pre_tool_call()`:

```python
# 1. Only gates terminal — ignores all other tools
if tool_name not in _COMMAND_TOOLS:  # ONLY {"terminal"}
    return None

# 2. Extracts the command string from args
command = _extract_command(args)
if not command:
    return None

# 3. Checks if this is a mesh session
if not _is_mesh_session():
    return None

# 4. Hardline floor — reuses Hermes' detect_hardline_command ✓
# 5. Dangerous check — reuses Hermes' detect_dangerous_command ✓
# 6. If dangerous → OPENS ITS OWN GATE on the mesh control server
approved, verdict = _gate_command(session_key, tool_name, command, description)
```

---

## Where they diverge (the gaps)

### Gap 1: Only `terminal` is gated — everything else passes free

The plugin only gates `_COMMAND_TOOLS = {"terminal"}`. Hermes' approval system potentially gates ANY tool via plugin `pre_tool_call` hooks returning `{"action": "approve"}` — see `request_tool_approval()` which accepts any `tool_name`. But the mesh-tool-gate plugin's own scope is hardcoded to `terminal` only.

**What this means:** `write_file`, `patch`, `execute_code`, `terminal` with `pty=True`, process management calls — all pass through ungated if they don't go through `terminal`. A model on the mesh could use `write_file` to overwrite `~/.ssh/authorized_keys` or `~/.hermes/.env` with zero approval. Hermes' own gateway would gate these via path-sensitive patterns in DANGEROUS_PATTERNS (e.g. `_SENSITIVE_WRITE_TARGET` which covers `~/.ssh`, `/etc/`, `~/.hermes/.env`), but the mesh plugin never checks them because it only looks at `terminal`.

### Gap 2: No session/permanent allowlist — every dangerous command is a full round-trip

Hermes' gate checks:
1. **Session allowlist** first (`_session_approved`): if the user said `[s]ession` for `chmod 777`, all future chmod 777 calls in this session auto-approve.
2. **Permanent allowlist** (`config.yaml`): `[a]lways` persists across restarts.
3. Only then does it prompt the human.

The mesh gate does **none of this**. Every dangerous command opens a new gate on the mesh control server, with a 900s timeout, and the mesh operator must answer `/approve` or `/deny` every single time. There is no `[s]ession` or `[a]lways` — the control server's `request_approval` returns `"approve"` or `"deny"` but never stores that decision for future calls.

**What this means:** A multi-step task that uses `git push --force` 3 times generates 3 separate approval prompts, each waiting up to 900s. On Hermes, the operator answers `[a]lways` once and it's done.

### Gap 3: No YOLO mode

Hermes has `HERMES_YOLO_MODE` (frozen at import) that skips all dangerous-command checks (except hardline). The mesh gate has no equivalent. There is no way for the mesh operator to say "I trust this session, don't gate dangerous commands."

### Gap 4: The gate is external to Hermes — not the same `[o]/[s]/[a]/[d]` prompt

Hermes' approval is an internal gate: `submit_pending()` → `resolve_gateway_approval()` → unblock. The mesh gate is a completely separate HTTP round-trip to the bridge's control server (`/gate/notify` → `/approve` or `/deny` on the mesh → control server resolves). These are two independent systems running in parallel:

- Hermes' own gate sees the plugin return `{"action": "block"}` (not `"approve"`) — so it never reaches Hermes' approve/deny prompt.
- The mesh gate lives entirely in the plugin → control server → mesh operator loop.
- There's no way to use Hermes' `[a]lways` persistence because the mesh verdict never flows back into Hermes' approval state.

### Gap 5: No smart approval

Hermes has an auxiliary LLM that can auto-approve low-risk commands (e.g. `chmod` on a known-safe path) before the human ever sees the prompt. The mesh gate never calls this path because it bypasses Hermes' approval machinery entirely.

### Gap 6: Detection scope — the plugin imports `tools.approval` but might not have the full pattern set

The import is `from tools.approval import detect_hardline_command, detect_dangerous_command` — this works because the plugin runs inside the gateway process where `tools.approval` is on `sys.path`. But the plugin is pinned to `_COMMAND_TOOLS = {"terminal"}` — it only calls these detectors on `terminal` commands. The 47+ DANGEROUS_PATTERNS include patterns for `write_file` targets (sensitive paths), `patch` operations, etc., but the plugin never feeds those tools' args through the detector because it only inspects `terminal`.

### Gap 7 (cosmetic but telling): docstring says "default 120" but env says 900

The plugin docstring still says `MESH_GATE_TIMEOUT default 120`. The env default is 900. The control server's `DEFAULT_APPROVAL_TIMEOUT` is 120 in source but overridden by `HERMES_MESH_APPROVAL_TIMEOUT=900` in the running bridge's env. These three values drift independently.

---

## What the mesh gate does RIGHT

- **Hardline floor**: unconditionally blocks no-recovery commands (rm -rf /, mkfs, dd, fork bomb, kill -1, shutdown). This IS Hermes' `detect_hardline_command` and it works correctly.
- **Fail-closed**: any error in the gate path → deny. No silent execution.
- **Bounded timeout**: the 900s gate wait doesn't wedge the gateway event loop (the hook is off the loop).
- **Pre-execution**: tools are stopped BEFORE they run, not after (unlike the old `agent:step` veto).

---

## Root cause: architectural fork

The plugin should return `{"action": "approve", "message": ..., "rule_key": ...}` to Hermes' `_authorized_dispatch`, which would then call `request_tool_approval()` — the SAME gate Telegram uses. That's what the docstring describes, but the actual code doesn't do it. Instead it:

1. Classifies the command using Hermes' detectors ✓
2. Then opens its OWN gate on the mesh control server ✗
3. Blocks the tool with `{"action": "block"}` if the mesh operator denies ✗

This is a parallel approval system that reuses Hermes' detection but none of its approval state, persistence, or UX.

---

## The fix: return `approve` directive, let Hermes gate it

The correct path for dangerous (non-hardline) commands:

```python
# Instead of:
approved, verdict = _gate_command(...)
if not approved:
    return {"action": "block", ...}
return None

# Do:
return {
    "action": "approve",
    "message": f"Dangerous command: {description}\nCommand: {command}",
    "rule_key": pattern_key,  # stable key for [a]lways allowlisting
}
```

This tells Hermes: "escalate this to the human approval gate." Hermes then:
1. Checks session/permanent allowlists
2. Calls `submit_pending()` + notifies the gateway callback
3. The gateway renders the `[o]/[s]/[a]/[d]` prompt

**The missing piece:** the gateway prompt currently renders on Telegram/Discord. For the mesh, the `register_gateway_notify` callback needs to push the approval prompt to the mesh operator over LXMF, and the operator's `/approve`/`/deny` reply needs to flow into `resolve_gateway_approval()`. That's the transport-specific half — but it's a gateway callback, not a separate HTTP control server.

This is the architecture the skill doc describes but the implementation diverged from. The `/gate/notify` control-server endpoint + `_gate_command` HTTP round-trip is a second approval system running in parallel with Hermes' own.

---

## Verified: No Hermes Agent modifications needed

The `pre_tool_call` → `approve` directive → `request_tool_approval` path is **already complete and shipped** in Hermes Agent. Traced from source at commit time of this audit:

### Step 1: Plugin returns `{"action": "approve", ...}`

`hermes_cli/plugins.py::_get_pre_tool_call_directive_details()` (line 6589) iterates `pre_tool_call` hook results. On `action == "approve"`, it extracts `message` and `rule_key` and returns a `_PreToolCallDirective(action="approve", message=..., rule_key=...)`.

### Step 2: Hermes resolves the directive

`agent/tool_executor.py::_authorized_dispatch()` (line 612) calls `_dispatch_pre_tool_call_hooks()` which calls `_resolve_block_from_details()` (line 6771):

```python
def _resolve_block_from_details(details, tool_name, ...):
    if details.action == "block":
        return details.message
    if details.action == "approve":
        # Calls request_tool_approval() — the SAME function Telegram uses
        result = request_tool_approval(
            tool_name,
            details.message or "",
            rule_key=details.rule_key or tool_name,
        )
        if not result.get("approved"):
            return result.get("message") or "BLOCKED: ..."
    return None  # proceed
```

### Step 3: `request_tool_approval()` handles everything

`tools/approval.py::request_tool_approval()` (line 4156) calls `_run_approval_gate()` which:

1. Checks the **permanent allowlist** (`[a]lways` in `config.yaml`)
2. Checks the **session allowlist** (`[s]ession` — in-memory, per gateway session)
3. If not allowlisted → calls `submit_pending()` → fires `register_gateway_notify` callback
4. Blocks until the gateway callback resolves with `[o]nce`/`[s]ession`/`[a]lways`/`[d]eny`
5. Fail-closes on timeout or error

This is the exact same path Telegram and Discord use for dangerous command approval. The `rule_key` controls the allowlist grain — same `rule_key` → same `[a]lways` entry.

### What needs to be built (transport layer only)

A **gateway approval adapter** for mesh sessions. Pattern:

```python
# In the gateway, when a mesh session has a pending approval:
register_gateway_notify(mesh_session_key, callback)
# → callback pushes [o]/[s]/[a]/[d] prompt to mesh peer via LXMF
# → operator replies /approve or /deny on the mesh
# → mesh command handler calls:
resolve_gateway_approval(mesh_session_key, "once"|"session"|"always"|"deny")
```

This is a transport adapter — same pattern as the Telegram adapter, Discord adapter, Slack adapter. It does NOT touch Hermes core. It wires the mesh as a new platform for the existing approval gate.

The mesh-tool-gate plugin's `/gate/notify` → control server → HTTP round-trip becomes dead code. The verdict flows through Hermes' own `resolve_gateway_approval()` instead.

### What the mesh-tool-gate plugin changes to

```python
def on_pre_tool_call(tool_name, args, session_id, **kwargs):
    # Hardline floor — stays unchanged (block outright) ✓
    is_hardline, desc = detect_hardline_command(command)
    if is_hardline:
        return {"action": "block", "message": f"BLOCKED (hardline): {desc}"}
    
    # Dangerous — return approve directive, let Hermes gate it
    is_dangerous, pattern_key, description = detect_dangerous_command(command)
    if is_dangerous:
        return {
            "action": "approve",
            "message": f"Dangerous command: {description}\nCommand: {command}",
            "rule_key": f"terminal:{pattern_key}",  # stable for [a]lways
        }
    
    return None  # safe — proceed
```

The `_gate_command()` HTTP round-trip and the `/gate/notify` control-server endpoint are removed.

---

## Priority

**High.** The current gate only covers `terminal` commands. `write_file` to sensitive paths, `patch` on config files, and `execute_code` calls all pass through completely ungated on the mesh. On Telegram/Discord these would be caught by Hermes' own path-sensitive patterns in DANGEROUS_PATTERNS.

**Implementation effort:** The plugin change is ~20 lines (swap `_gate_command` block for `{"action": "approve"}` return). The gateway approval adapter is new work — wire the mesh as a notification target for `register_gateway_notify` and route `/approve`/`/deny` mesh commands into `resolve_gateway_approval`. No changes to Hermes Agent core.
