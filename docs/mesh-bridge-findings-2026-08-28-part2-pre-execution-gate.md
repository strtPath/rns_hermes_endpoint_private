# Pre-execution tool gate — feasibility & design (2026-08-28)

## The question
Hamza's deal-breaker for the mesh bridge: no live tool-output stream and no
pre-execution command gate (Tirith-style approve/deny) like Telegram has.
Is a **true pre-execution** gate (approve/deny *before* the tool runs, not
acknowledge-then-veto after the fact) actually possible?

## Answer: YES — Hermes has a first-class pre-execution hook
`pre_tool_call` fires **in-process, before dispatch**, in
`agent/tool_executor.py::_authorized_dispatch`:

```
def _authorized_dispatch(final_args):
    ...
    block_message = scope_block
    if block_message is None:
        def _resolve_pre_tool_block():
            from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
            block_msg, modified_args = _dispatch_pre_tool_call_hooks(
                function_name, final_args, ...)
            if modified_args is not None:
                final_args = modified_args   # ← args can be rewritten
            return block_msg
        block_message = _resolve_pre_tool_block()   # or via authorization_gate
    # then guardrails, then execute(final_args)
```

A `pre_tool_call` hook can return one of:
- `{"action": "block", "message": "..."}` — vetoes the call outright; the
  message becomes the tool result the model sees. **The tool never runs.**
- `{"action": "approve", "message": "...", "rule_key": "terminal:rm"}` —
  ESCALATES to the existing human-approval gate (same mechanism as Tier-2
  dangerous shell patterns on Telegram). Forces a `[o]nce/[s]ession/[a]lways/
  [d]eny` decision on ANY tool, not just terminal.
- `{"action": "modify", "args": {...}}` — rewrites tool_input before dispatch.

The hook receives: `tool_name`, `args`, `task_id`, `session_id`,
`tool_call_id`, `turn_id`, `api_request_id`, `middleware_trace`.

This is the exact mechanism `plugins/security-guidance/__init__.py` already
uses in block mode (`_on_pre_tool_call` → `{"action":"block", ...}`).

## Why this changes the bridge design
The current `mesh-tool-events` hook is **acknowledge-then-veto**: it fires on
`agent:step` / `agent:start`, classifies safe/risky, POSTs to the control
server, and can only *veto after* the tool has started (or already run). That
is the wrong model for a control surface.

With `pre_tool_call` the bridge plugin can:
1. **Before any tool executes**, classify the call (safe / risky / dangerous).
2. For risky/dangerous: return an `approve` directive → the human gets an
   approve/deny prompt on the mesh (LXMF) *before* the tool runs — true
   pre-execution, matching Telegram's Tirith behavior.
3. For dangerous: return `block` (hard veto) or `approve` (human decides).
4. For safe: return None (proceed) — or, for full transparency, `approve`
   everything and let the human watch each step (step-through mode).

Because it's a **plugin** (in-process), it works on the mesh bridge the same
way it works on Telegram — the gate is not transport-specific. The only
transport-specific piece is *where* the approve/deny prompt is rendered
(LXMF vs Telegram vs TUI).

## CONFIRMED: the `approve` directive gives exactly the Tirith UX
Verified in `hermes_cli/plugins.py::resolve_pre_tool_block` /
`_resolve_block_from_details` (the single fail-closed entry point every
dispatch path funnels through):

```
details = _get_pre_tool_call_directive_details(...)   # block | approve | None
if details.action == "block":
    return details.message            # → tool result = block message; tool never runs
if details.action == "approve":
    result = request_tool_approval(tool_name, message, rule_key)
    # ↑ THE SAME GATE as Tier-2 dangerous-shell on Telegram:
    #   [o]nce / [s]ession / [a]lways / [d]eny
    if not result.get("approved"):
        return result.get("message") or f"BLOCKED: plugin approval required"
    # approved → returns None → tool proceeds
# fail-closed: approve gate that errors / denies / times out → BLOCK
```

Key properties for our requirements:
- **Pre-execution**: fires in `_authorized_dispatch` before `execute(final_args)`.
- **Fail-closed by construction**: an `approve` directive whose gate errors,
  denies, or times out becomes a block — matches Hamza's "fail-closed on
  timeout" decision exactly. No extra code needed.
- **`rule_key`** gives per-tool `[a]lways` allowlist grain (e.g.
  `terminal:git`, `write_file:<home>`).
- **`modify`** directives rewrite `args` before dispatch (visible even if a
  later hook blocks).

So the bridge plugin's `pre_tool_call` handler just classifies and returns
the right directive; Hermes' existing approval-gate machinery (which already
works on Telegram and is fail-closed) does the rest. The transport-specific
piece is making the mesh render the `[o]/[s]/[a]/[d]` prompt.

## Implementation path
1. Convert `~/.hermes/hooks/mesh-tool-events/` (shell hook, acknowledge-
   then-veto) into a **Python plugin** `pre_tool_call` handler:
   - classify tool + args (reuse existing risk rules),
   - for risky: POST to control server (127.0.0.1:8471) and **block on the
     human's response** (poll/long-poll) → return approve-or-block based on
     the verdict;
   - for safe: return None (proceed) or, if step-through is ON, `approve`.
2. The control server already has the approve/deny + step-through +
   hold-state plumbing (built for acknowledge-then-veto). Reuse it; the
   plugin just changes *when* it consults it (pre-dispatch vs post-start).
3. Live tool-output stream: keep the `agent:step` observer for streaming
   output to the mesh (that part stays acknowledge-style — it's
   informational). The *gate* moves to `pre_tool_call`.

## Open questions
- Does the human approve/deny prompt have a transport on the mesh that can
  render an interactive approve/deny? (Control server endpoint exists; the
  LXMF client needs an affordance to send the verdict.)
- Timeout semantics: if the human never answers, fail-closed (block) or
  fail-open (proceed)? Recommend fail-closed for dangerous, configurable for
  risky.
- `rule_key` for `[a]lways` allowlists: define per-tool grain
  (e.g. `terminal:git`, `write_file:<home>`).