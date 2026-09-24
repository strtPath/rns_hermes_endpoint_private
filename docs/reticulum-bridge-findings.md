# Reticulum Bridge — Known Issues & Required Fixes for Upstream PR

This document records issues discovered while using the Hermes-Reticulum bridge, and the changes needed in upstream `rns_hermes_endpoint` to address them. These are the gaps that prevent full functionality and should be documented for a pull request.

---

## Issue 1: "Unknown toolsets" Warning on Every Response

### Symptom
Every response sent through the Reticulum bridge starts with a warning message:
```
Warning: Unknown toolsets: messaging, moa?
```

### Root Cause
The Hermes gateway's `platform_toolsets` configuration in `config.yaml` references two toolset names that don't exist in the core Hermes codebase (`toolsets.py`):

- **`messaging`** — Not defined in TOOLSETS dict or registered by any plugin
- **`moa`** — Likely "multi-agent orchestration" (an obsolete/removed concept)

The gateway's `cli.py` validates toolset names at startup and prints this warning when it encounters unknown names. The bridge inherits this config, so every LXMF response carries the warning prefix.

### Fix Needed in `rns_hermes_endpoint`
1. **Remove invalid entries** from the platform_toolsets configuration for the reticulum platform adapter, OR
2. **Create a dedicated toolset** (`hermes-reticulum`) that includes only `_HERMES_CORE_TOOLS` without referencing unknown names
3. The bridge's plugin `__init__.py` should register itself with a clean toolset definition that doesn't depend on legacy config values

### Fix Needed in Upstream Hermes (optional)
The gateway could be more graceful about unknown toolsets — silently skip them rather than printing the warning, since it already handles missing tools correctly. Alternatively, provide a setup command (`hermes tools`) to auto-detect and suggest fixes for invalid toolset references.

---

## Issue 2: Clarify/Accept-Denial Prompts Not Appearing in Telegram

### Symptom
When Hermes needs user input (via `clarify` tool) or when the agent wants to execute a potentially risky command (accept/deny UI), these interactive prompts do not appear in the Telegram client. The bridge sends plain text replies only, and the user sees no buttons or inline choices.

### Root Cause
The LXMF protocol is fundamentally a simple text messaging system — it has no concept of:
- Inline button widgets
- Accept/denial confirmation dialogs  
- Multi-choice interactive prompts

The Hermes `clarify` tool generates these interactive elements (multiple choice, open-ended questions), and the gateway normally renders them as Telegram inline keyboards or Discord buttons. However, LXMF messages are plain text only — there's no transport mechanism to carry structured prompt data from the bridge back to the user.

### Fix Needed in `rns_hermes_endpoint`
1. **Detect clarify prompts** in the Hermes response and convert them to a readable text format that users can respond to via plain LXMF text (e.g., numbered options with clear instructions)
2. **Add an "interactive mode" config option** — when enabled, the bridge formats clarify dialogs as:
   ```
   [CLARIFY] Question here:
   1) Option A
   2) Option B  
   3) Other (type your answer)
   Reply with a number or type your own.
   ```
3. **Handle accept/deny prompts** — detect when the agent proposes terminal commands that need approval, and format them as text requests for user consent:
   ```
   [ACTION REQUIRED] Agent wants to run: `sudo reboot`
   Reply "yes" to approve or "no" to deny.
   ```

---

## Issue 3: No Structured Input Handling from LXMF Users

### Symptom (related to Issue 2)
When a user tries to respond to a clarify prompt via LXMF, the bridge treats their response as plain text conversation rather than structured input for the clarify tool. There's no mechanism to map LXMF text responses back to the original clarify context.

### Fix Needed in `rns_hermes_endpoint`
1. **Maintain clarify session state** — track which LXMF conversation is waiting for a clarify response, and when a new message arrives from that user, check if it matches a pending prompt's options
2. **Parse numbered responses** — detect patterns like "1", "Option B", etc., and map them back to the original clarify context
3. This requires state management in the bridge adapter that persists between messages

---

## Summary of Required Changes for Upstream PR

| Issue | What Needs Changing | Where |
|-------|-------------------|-----|
| Unknown toolsets warning | Clean up platform_toolsets config, create `hermes-reticulum` toolset | Bridge plugin registration + upstream Hermes gateway |
| No interactive prompts in LXMF/Telegram | Add clarify prompt detection + text formatting for plain messaging transport | `plugin/adapter.py` and `core/hermes_client.py` |
| No structured input handling | Add stateful context tracking for clarify sessions over LXMF | `plugin/adapter.py` (new session management code) |

---

## Additional Observations

### Socket Reconnection Warnings
The log shows repeated "Socket for LocalInterface[rns/default] was closed, attempting to reconnect..." messages. This suggests the RNS connection isn't stable — likely due to:
- rnsd service restarts causing socket drops
- Firewall or network issues on localhost connections
- The bridge not properly handling reconnection after gateway restarts

This is a separate stability concern but worth documenting for upstream awareness.

### Identity Validation
LXMF messages from the user's ID show "link, sig=invalid/unknown" — this means the LXMF stamp signature isn't being verified correctly, or the sender's identity needs to be explicitly trusted in the ACL. The bridge does have an ACL layer (`hermes_reticulum.core.acl`) but it may not be properly configured for your specific user ID hash.
