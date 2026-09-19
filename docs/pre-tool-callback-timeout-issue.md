# Pre-tool callback timeout issue

## Symptom
When running tests or executing terminal commands in the rns_hermes_endpoint project, tool calls hang or are blocked with the error:

```
Error: pre_tool_call plugin callback timed out or is still running
```

The next tool call (terminal, execute_code, write_file, etc.) fails with the same message. Multiple retries all fail.

## When it happens
- After a long-running test or command completes (or times out)
- The next tool call hangs or is blocked
- Multiple retries all fail with the same error

## Root cause — the actual mechanism

This is NOT a test-suite issue. It is Hermes' `pre_tool_call` hook
timeout interacting with the `mesh-tool-gate` plugin's 900s blocking wait.

### How `pre_tool_call` dispatch works

Every `pre_tool_call` hook callback runs on a **daemon worker thread**
with a wall-clock cap of `plugins.hook_callback_timeout` (default **30s**,
max 600s). The caller does `done.wait(timeout=30)`:

- **If the worker finishes in <30s** → its result is used (block/approve/modify).
- **If the worker is still running at 30s** → the worker is **abandoned
  (never joined)**. The callback is marked "running" in
  `_hook_running_callbacks`, and a 60s suppression window starts
  (`_HOOK_TIMEOUT_SUPPRESSION_SECONDS = 60`).

Because `pre_tool_call` is in `_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS`,
the timeout produces a **fail-closed block directive**:

```
{"action": "block", "message": "pre_tool_call plugin callback timed out or is still running"}
```

### Why it keeps happening

The `mesh-tool-gate` plugin's `on_pre_tool_call` does a **blocking HTTP POST**
to the mesh control server with a **900s timeout**:

```python
_GATE_TIMEOUT = float(os.environ.get("MESH_GATE_TIMEOUT", "900"))
# ...
with urllib.request.urlopen(req, timeout=_GATE_TIMEOUT) as resp:
```

When the tool is gated (dangerous command detected), the plugin blocks
for up to 900s waiting for the operator's verdict. The 30s Hermes timeout
fires long before the gate resolves.

**The chain:**

1. A gated tool call (e.g. `terminal` with a dangerous command) fires
   `pre_tool_call`.
2. `mesh-tool-gate.on_pre_tool_call` blocks on the 900s HTTP wait.
3. At 30s, Hermes abandons the worker, marks the callback "running",
   starts a 60s suppression window, and returns the fail-closed block.
4. The operator sees the gate on the mesh and responds (`/approve` or
   `/deny`).
5. The gate resolves — the abandoned worker's `urlopen` returns.
6. The worker finishes and removes itself from `_hook_running_callbacks`.
   But the 60s suppression window may still be active, or a new tool call
   arrives before the window clears.
7. **If a new `pre_tool_call` fires while the suppression window is active
   or the callback is still marked "running"** → the callback is skipped
   (returns `_HOOK_SKIPPED`) → fail-closed block again.
8. The operator's `/approve` or `/deny` response is consumed by the
   abandoned worker (the HTTP response is already in flight), so the
   operator's verdict has no effect on the current tool call — it was
   already blocked at step 3.

**Net effect:** the operator's approve/deny is racing against the 30s
Hermes timeout. If the operator responds after 30s (which is the normal
case — 900s gate window), the tool is **already blocked**. The operator's
response only clears the gate for the *next* tool call, and even then only
if the 60s suppression window has expired.

### Why it's worse after long commands

After a long-running `terminal` command (e.g. a test suite), the next tool
call fires `pre_tool_call`. If the previous call's gate worker is still
abandoned-and-suppressed, the new call is immediately blocked. The operator
may have already approved the previous call, but the suppression window
(60s) means the next call within that window is auto-blocked.

## The two timeouts that must be reconciled

| Layer | Timeout | Config |
|-------|---------|--------|
| Hermes `pre_tool_call` hook | **30s** (default), max 600s | `plugins.hook_callback_timeout` in config.yaml |
| `mesh-tool-gate` HTTP wait | **900s** (default) | `MESH_GATE_TIMEOUT` env |
| Control server gate wait | **900s** (default) | `HERMES_MESH_APPROVAL_TIMEOUT` env |

The 30s hook timeout is **always** shorter than the 900s gate timeout.
This means: **any gated tool call that waits >30s for the operator is
guaranteed to be fail-closed blocked**, regardless of what the operator
does. The operator's approve/deny is too late.

## Fix options

### Option A: Raise `plugins.hook_callback_timeout` (config.yaml)

```yaml
plugins:
  hook_callback_timeout: 900
```

This makes the Hermes hook timeout match the gate timeout. The worker is
no longer abandoned at 30s — it waits up to 900s for the operator's
verdict. The gate resolves, the worker returns the real verdict
(approve → proceed, deny → block with reason), and the operator's
response is respected.

**Risk:** a truly hung gate (control server unreachable, operator AFK)
now blocks the tool call for up to 900s before fail-closed. The
operator sees the gate on the mesh and can deny. If the control server
is down, the `urlopen` fails immediately (connection refused) and the
gate fails closed fast — no 900s wait.

**This is the correct fix** — it aligns the two timeouts so the operator's
verdict is actually reachable.

### Option B: Make `mesh-tool-gate` non-blocking (async)

Instead of blocking on `urlopen(req, timeout=900)`, the plugin could:
1. Fire the gate request.
2. Return `{"action": "approve"}` immediately (let the tool run).
3. Register a `post_tool_call` hook that checks whether the gate
   resolved. If denied, the tool already ran — too late.

This doesn't work because the whole point of the gate is to **stop the
tool before it runs**. A non-blocking gate can't block the tool.

### Option C: Reduce `MESH_GATE_TIMEOUT` to <30s

This defeats the purpose — the operator needs time to read the gate,
think, and respond. 30s is too short for a real approval decision,
especially over LoRa with hop latency.

### Recommendation

**Option A, but with the 600s hard clamp in mind.** Set `plugins.hook_callback_timeout`
in the Hermes config to just above the gate timeout and below the 600s hard clamp
(`_MAX_HOOK_CALLBACK_TIMEOUT_SECS = 600.0`). The clamp is a wall you cannot raise:
a value like 900 is silently clamped to 600 at runtime, so **a 900s gate is impossible**
on this code path, no matter what you set.

A concrete safe alignment (the shipped reference, see the README Deployment section):

```env
# .env
HERMES_MESH_APPROVAL_TIMEOUT=480
MESH_GATE_TIMEOUT=480
```

```yaml
# ~/.hermes/config.yaml
plugins:
  hook_callback_timeout: 490   # must be > MESH_GATE_TIMEOUT and < 600
```

This makes the control-server deny clock (480s) fire before the Hermes hook
wrapper, so an unanswered gate produces the clean `BLOCKED: The requested
action did not receive approval before the gate timed out` message (which
also allows the model to continue on safe tools) instead of the raw
`pre_tool_call plugin callback timed out or is still running` wedge.

**The two values that MUST be reconciled are the three-layer ordering, not
a specific number:**
`HERMES_MESH_APPROVAL_TIMEOUT <= MESH_GATE_TIMEOUT < hook_callback_timeout < 600`.

The only scenario where the tool blocks for the full gate window is a truly
unanswered gate — the intended fail-closed behavior. A shorter gate window
(fast link) cuts the worst-case block.

## Public deployment note

This project is intended for public use. The gate has **three** coupled
timeouts that must satisfy a specific ordering (the third layer lives in
Hermes core and has a hard ceiling):

| Setting | Location | Default | Constraint |
|---------|----------|---------|------------|
| `HERMES_MESH_APPROVAL_TIMEOUT` | bridge `.env` | 900s | the real deny-by-default clock |
| `MESH_GATE_TIMEOUT` | bridge `.env` | 900s | must be `>= HERMES_MESH_APPROVAL_TIMEOUT` |
| `plugins.hook_callback_timeout` | Hermes `config.yaml` → `plugins:` | 30s | must be `> MESH_GATE_TIMEOUT` and `< 600` (hard clamp) |

If `hook_callback_timeout` < `MESH_GATE_TIMEOUT`, any gated tool call that
waits longer than `hook_callback_timeout` for the operator's verdict is
**fail-closed blocked** — the operator's `/approve` arrives too late. The tool
is blocked even though the operator approved it. And if
`MESH_GATE_TIMEOUT` < `HERMES_MESH_APPROVAL_TIMEOUT`, the plugin's POST fails
closed before the control clock — the operator's window is silently capped.

**The shipped code defaults (900/900/30) are NOT safe on their own** — the 30s
hook wrapper fires long before a 900s gate resolves. Public users MUST set all
three on deployment with the ordering above. The bridge refuses to start if
`MESH_GATE_TIMEOUT < HERMES_MESH_APPROVAL_TIMEOUT` (v0.21.2+). See the README
Deployment section for the full recipe.

The `mesh-tool-gate` plugin (and any other plugin registering a
`pre_tool_call` hook) inherits this behavior — the hook timeout is
global, not per-plugin. Any `pre_tool_call` callback that blocks longer
than `hook_callback_timeout` is abandoned and fail-closed.

See [pre-tool-callback-timeout-issue.md](pre-tool-callback-timeout-issue.md)
for the full mechanism, the race-condition analysis, and fix options.
