# Mesh tool gate: unanswered approval wedges all tools for the rest of the turn

**Documented 2026-09-18.**

## Symptom

The rns bridge ran autonomously. A `terminal` tool call in a mesh session
escalated to the human gate (`/approve` pushed to the operator's mesh peer).
The operator was AFK and never answered. Instead of timing out and telling the
model to adapt (as Telegram does), the session wedged: **every subsequent tool
call failed** with

```
pre_tool_call plugin callback timed out or is still running
```

The model then reported it "couldn't do any tools or anything" for the rest of
the turn.

## Timeline (2026-09-18, session 20260918_015727_ae1925)

| Time | Event |
|---|---|
| 03:16:29 | `mesh-tool-gate: Jev triage apply mode='allow_benign' tool=terminal risk=0.23 stakes='sensitive' handling='escalate' conf=0.66 → ESCALATE to human gate (return='escalate')` |
| 03:16:29 | `Gate-open pushed for terminal → mesh peer (operator, identity redacted)` |
| 03:16:29 → 03:24:02 | Operator AFK — no `/approve` or `/deny` received for ~7 min |
| ~03:23:28 | First `pre_tool_call` hook callback **abandoned** by the Hermes hook wrapper (600 s timeout fired); the tool is fail-closed with the raw timeout message |
| 03:24:02 | `WARNING hermes_cli.plugins: Hook 'pre_tool_call' callback on_pre_tool_call skipped after previous timeout or while still running` (repeats 5× through 03:25:10) |
| 03:24:53 | `Tool read_file returned error: {"error": "pre_tool_call plugin callback timed out or is still running"}` |
| 03:25:10 | `Tool clarify returned error` (same) |
| 03:26:04 | Turn ended (`text_response`), 31 API calls / 69 tool turns — all tool attempts after the wedge failed |

The plugin's blocking `urlopen` POST kept running until the control server's
deny clock fired (~03:26:20, 590 s after the gate opened), but by then the
gateway had already handed the agent a raw timeout message and suppressed the
hook. The operator's (never-sent) verdict went nowhere.

## Root cause — the gate never blocks the tool; it blocks the *hook*

Two coupled facts:

1. **The plugin's blocking POST outlives the hook wrapper.** The mesh-tool-gate
   `pre_tool_call` hook does a blocking `urlopen` POST to the control server
   with `MESH_GATE_TIMEOUT=590`. Hermes runs `pre_tool_call` in a daemon worker
   thread with `hook_callback_timeout=600` (`~/.hermes/config.yaml`). When the
   operator is AFK past 600 s, the wrapper **abandons the worker thread**
   (fail-closed, by design for `pre_tool_call`) and returns the raw timeout
   message to the agent. The abandoned thread keeps its `urlopen` for the
   remaining ~10 s, then exits — but no one is listening for its verdict.

2. **Suppression then fail-closes *every* tool for the rest of the turn.**
   `_HOOK_TIMEOUT_SUPPRESSION_SECONDS = 60` (Hermes core) suppresses re-firing
   the same callback for 60 s after a timeout. The suppression is on the hook
   *invocation itself*, so every subsequent `pre_tool_call` in the session is
   skipped and fail-closed (`pre_tool_call` is the only fail-closed hook). The
   agent sees `pre_tool_call plugin callback timed out or is still running` on
   *all* tools — not just the one that was gated. This is the "couldn't do any
   tools" symptom.

So the 590/590/600 three-layer alignment is *almost* correct, but the inner
layers (590 s) still fire **before** the outer backstop (600 s) in the AFK case:
the hook wrapper abandons the plugin at 600 s, before the control server's
deny clock (590 s) + plugin `urlopen` would have produced a clean
`BLOCKED: Action timed out without user response` message. The agent gets the
raw wrapper message instead of the gateway-aligned BLOCK message, and then the
suppression wedges the rest of the turn.

Contrast with Telegram: Telegram's approval flow resolves the gate *in the
gateway process* (the operator's `/approve`/`/deny` hits the same process that
owns the pending gate), so the tool returns a clean `BLOCKED` message and the
turn continues. The mesh gate resolves in a *separate bridge process* over the
network, which is why the 600 s wrapper timeout can beat the 590 s deny clock
and strand the tool.

## Why the model "couldn't do any tools"

- The first gated tool: raw timeout message (hook wrapper abandoned it).
- Every tool after that: the 60 s suppression + "previous timeout or still
  running" guard fail-closes the `pre_tool_call` hook, so the tool never runs.
- The model, seeing every tool error with the same message, concludes it
  cannot act and ends the turn with a text response.

## The fix

**Immediate (config-only, no code change):** shorten the operator window so the
control-server deny clock fires *before* the 600 s hook wrapper, AND lower the
hook wrapper below the gate timeout so the wrapper never fires first. The
three layers must satisfy: `layer1 < layer2 < layer3` **and** `layer3 < 600`
(hard clamp). Practically:

- `HERMES_MESH_APPROVAL_TIMEOUT=480` (control server deny clock)
- `MESH_GATE_TIMEOUT=480` (plugin `urlopen`)
- `plugins.hook_callback_timeout: 490` (gateway backstop — still < 600 clamp,
  and > 480 so it never fires first)

This makes the clean `BLOCKED: Action timed out without user response` message
win over the raw wrapper timeout in the AFK case, and bounds the worst-case
"every tool blocked" window to ~480 s (8 min) instead of 600 s. The turn still
ends with a text response, but the model gets the proper instruction and the
suppression is bounded.

**Structural (recommended, code change):** the hook-wrapper model is
fundamentally wrong for a network-resolved gate. The plugin should NOT block
for the operator window inside the `pre_tool_call` hook. Instead:

1. On `pre_tool_call`, the plugin POSTs to the control server **non-blocking**
   (or with a short timeout) and returns a **lightweight block** immediately:
   `"Gate pending: your /approve is in flight; this tool will not run until
   the operator decides. Do not retry it this turn."`
2. The bridge (which owns the control server) becomes the resumer: when the
   operator's `/approve` lands, the bridge re-drives the pending tool call in
   the mesh session (or re-issues the turn with a note that the tool was
   approved). This is the "bridge-driven resume" path flagged in
   `references/approval-gate-timeout.md`.
3. The 600 s hook-wrapper timeout and the 60 s suppression no longer interact
   with the operator window, because the plugin returns in milliseconds.

This is a design change, not a tuning knob. Ship the 480/490 alignment first;
build the resume path if 8 min of AFK still bites in the field.

## Relationship to prior findings

- `references/approval-gate-timeout.md` (2026-09-05): documented the 30 s →
  600 s hook-wrapper fix and the 590/590/600 alignment. **It missed the AFK
  case** where the 600 s wrapper fires *before* the 590 s deny clock resolves
  the gate, because the 590/600 gap was assumed safe. It is NOT safe when the
  operator is AFK past 590 s: the wrapper abandons the plugin and the
  suppression wedges the turn.
- `references/pre-tool-callback-stall.md` (2026-09-04): documented the *stall*
  when `/a` or `/d` arrives mid-POST. This is the *unanswered* case (no
  verdict at all), which is worse: no resolution, just a wedge.
- `docs/mesh-bridge-findings-2026-08-29-deny-veto-retry-loop.md` (commit
  70bfcb5): fixed the deny-veto re-fire loop. Unrelated but same subsystem.

## Verification

```bash
# Bridge env (should show 480/480 after the fix is applied):
tr '\0' '\n' < /proc/$(pgrep -f 'hermes-reticulum run')/environ | grep -E 'GATE_TIMEOUT|APPROVAL_TIMEOUT'
# Hook wrapper (should be 490 after the fix):
grep -A1 '^plugins:' ~/.hermes/config.yaml | grep hook_callback_timeout
# Confirm the wedge signature in a future incident:
grep -E 'pre_tool_call.*skipped after previous timeout|pre_tool_call plugin callback timed out' ~/.hermes/logs/agent.log
```

## Status

- **Config fix (480/490): APPLIED 2026-09-18.** `.env` now reads
  `HERMES_MESH_APPROVAL_TIMEOUT=480`, `MESH_GATE_TIMEOUT=480` (backup saved to
  `.env.bak.*`); `~/.hermes/config.yaml` `plugins.hook_callback_timeout: 490`.
  Takes effect on next bridge + gateway restart (neither was running at apply
  time). Verify with the commands in the Verification section (bridge env →
  480/480, config → 490).
- **Structural fix (bridge-driven resume): NOT STARTED.** Design change:
  plugin returns a lightweight block immediately, bridge re-drives the gated
  tool on `/approve`. Build only if 8 min of AFK still bites in the field.
