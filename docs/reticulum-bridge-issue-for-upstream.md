# Reticulum Bridge — Issues Found in Production Use

I'm using the Hermes-Reticulum bridge (rns_hermes_endpoint) and found two bugs that break normal operation. Here's what happens, why it breaks, and what needs to change.

---

## Bug 1: "Unknown toolsets" Warning Prints on Every Response

### What you see
Every message sent through the bridge starts with this prefix:
```
Warning: Unknown toolsets: messaging, moa?
```

### Why it happens
The bridge plugin registers itself under `platform_toolsets` in config.yaml. The CLI entry there lists two toolset names — `messaging` and `moa` — but neither exists anywhere in Hermes' core toolset registry (`toolsets.py`). When the gateway validates these at startup, it prints the warning into every outgoing message because the bridge inherits this broken configuration.

### What needs to change
The bridge plugin's platform registration should define its own clean toolset (something like `hermes-reticulum`) that includes only `_HERMES_CORE_TOOLS` without referencing unknown names. Alternatively, remove `messaging` and `moa` from config.yaml — they're not registered anywhere in the codebase and serve no purpose.

---

## Bug 2: Clarify Prompts and Accept/Deny Dialogs Don't Render

### What you see
When Hermes needs user input (via the `clarify` tool) or wants to run a potentially risky command that needs approval, those interactive elements — multi-choice buttons, accept/denial UI, inline keyboard prompts — don't appear in your Telegram client. You just get plain text with no way to interactively respond.

### Why it happens
LXMF is a plain-text-only messaging protocol. It has no concept of inline buttons, structured choice widgets, or confirmation dialogs. The Hermes `clarify` tool and action approval system normally render these as platform-specific UI (Telegram inline keyboards, Discord buttons), but LXMF can't carry that structured data. The bridge doesn't detect when a response contains a clarify prompt and convert it to a readable text format that users can reply to via plain LXMF messaging.

### What needs to change
The bridge adapter needs two additions:

1. **Clarify detection + formatting** — When the Hermes response contains a `clarify` tool call, detect it in the output and reformat it as numbered options people can respond to over plain text. Something like:
   ```
   [CLARIFY] Which deployment target?
   1) staging
   2) prod
   Reply with a number or type your own answer.
   ```

2. **Session state tracking** — Track which LXMF conversation is waiting for a clarify response, and when the user replies via text, parse it back into the original clarify context so Hermes understands whether they chose option "1" vs typed something custom. Without this, every reply just looks like normal conversation to the agent.

The same applies to action approval prompts — detect when the model wants to run a terminal command that needs user consent, and format them as text requests for approval instead of silently executing or failing.

---

## Bonus: Socket Reconnection Noise

The LXMF log shows repeated "Socket for LocalInterface[rns/default] was closed, attempting to reconnect..." messages after every gateway restart. The bridge should handle RNS reconnection gracefully rather than flooding logs — consider a startup handshake that waits for the socket to stabilize before processing incoming messages.

---

## Summary of Required Fixes

| Problem | Root Cause | What Needs Changing |
|---------|-----------|-------------------|
| Warning prefix on every response | config.yaml references toolsets (`messaging`, `moa`) that don't exist in core registry | Clean up platform_toolsets config or add a proper `hermes-reticulum` toolset to bridge plugin |
| No interactive prompts in plain-text transport | LXMF can't carry structured UI; bridge doesn't detect/format clarify dialogs for text-only replies | Add clarify detection, numbered option formatting, and session state tracking to plugin/adapter.py |
| User replies treated as conversation | No mechanism maps LXMF text responses back to original clarify context | Track pending clarify sessions in adapter and parse numbered/text responses |
