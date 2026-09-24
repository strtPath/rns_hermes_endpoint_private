# Findings: Tool call blocking is invisible to the mesh user

Date: 2026-09-05
Session: step-through mesh test

## Observation

During a step-through test over Reticulum, a tool call was blocked by the
command parser guard ("BLOCKED: Command flagged as dangerous"). The user,
watching from the other end of the mesh, received no indication that a
tool had been flagged and blocked. From the user's perspective the agent
simply continued to the next action.

## Why this matters

Step-through mode is explicitly for being watched. A blocked tool call is
a meaningful state change (the agent's intended action did not run).
If the watcher cannot see it, the mode loses its purpose: the user cannot
verify what actually executed versus what was suppressed.

## What's needed

- Surface blocked/flagged tool calls to the mesh user as distinct
  messages, same as successful tool outputs.
- The message should make clear the tool was flagged and what the
  guard said, so the user can decide if that's acceptable or if the
  guard needs tuning (approvals.single_query_mode, etc).
- Consider whether other non-success tool states (timeout, partial
  failure) also need explicit surfacing in step-through mode.

## Where to look

- The step-through delivery path in the bridge (rns_hermes_endpoint).
- The tool result envelope: "BLOCKED" / "flagged" status should be
  a first-class state that triggers a user-visible message, not just
  an inline status field.

## Open questions

- Should all tool states be surfaced, or only blocks?
- Should the user be able to approve the blocked command inline
  (interactive approval over the mesh)?
