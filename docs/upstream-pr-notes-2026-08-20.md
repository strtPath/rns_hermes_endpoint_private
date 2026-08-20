# Proposed upstream changes — rns_hermes_endpoint

PR-ready summary of changes made to the bridge and the fixes that belong
upstream. Workflow: develop and stabilize on the fork
(`strtPath/rns_hermes_endpoint`) first, then open the PR to the original
project. Complements the local findings log
(`docs/mesh-bridge-findings-2026-08-20.md`); this file is written to be
shareable (no local paths, no credentials).

---

## A. New: runtime slash-command layer

### What
Adds an extensible `/` command dispatcher to the LXMF bridge, mirroring the
Telegram gateway's control commands. Currently ships:

- `/model` — list available models, switch the active model, reset to default
  (pin persisted across restarts)
- `/new` — start a fresh conversation (drop prior context)
- `/help` / `/commands` — list all supported commands

### Why
The mesh bridge had no runtime control surface. Operators couldn't switch
models or reset a stuck conversation without restarting the service. The
27b-class local models can be slow or intermittently wedged, so an in-band
reset (`/new`) and model switch (`/model`) are the practical escape hatch.

### Design
- `core/commands.py` — `CommandDispatcher` + a `COMMANDS` registry. Each
  command is `(ctx, args) -> str | None`; returning `None` means "not a
  command, forward to the model." Adding a command = one dict entry + a
  small handler. No changes to the message flow or CLI.
- `core/model_command.py` — `/model` handler; persists the pin to a
  small state file under the LXMF storage dir.
- `core/hermes_client.py` — `HermesClient` gains:
  - model pinning (`-m <model>`; from env or runtime `set_model()`)
  - named session-thread continuity (`-c <name> --create-if-missing`)
  - `reset_session()` for `/new`

### Behavior change to flag
Mesh messages now resume a **named session thread** (continuity) rather than
each call being isolated. This is deliberate (it's what makes `/new` mean
something) but is a behavior change operators should know about.

---

## B. Bug fix: "Hermes Agent not found" from service `PATH`

### Symptom
Bridge replies with `❌ Hermes Agent not found. Check your installation.`
even though Hermes is installed and works from a shell.

### Root cause
The user systemd unit set no `PATH`, so the service inherited a minimal
session PATH that did not include the directory where the `hermes` binary
lives. `find_hermes_bin()` returned `None` and every `chat -q` failed at
binary lookup.

### Upstream recommendations
1. **Fail loudly at startup.** The client already raises `RuntimeError` when
   the binary is missing — the service should log a clear, actionable
   message (with the candidate paths searched) rather than degrading to a
   per-message error.
2. **Document pinning `PATH`** in the service unit (the unit should not rely
   on the user session's environment).
3. Optionally accept a `HERMES_BIN` override (already supported) and surface
   it in `--status`.

---

## C. Bug fix: timeout too tight for large local models

### Symptom
One-word messages time out (`⏱️ Processing exceeded the 600s limit`).

### Root cause
The 300s default is shorter than a large local model (e.g. a 27b GGUF via
llama.cpp) can take to produce a first token, and a hung model is indistinguishable
from a slow one until the full cap elapses.

### Upstream recommendations
- Make the timeout configurable per-deployment (env/flag) — already possible
  via `HERMES_TIMEOUT`, but document it.
- Consider a **first-token / liveness** timeout distinct from the total
  cap, so a wedged model is detected quickly instead of after the full limit.

---

## D. Still-open (from earlier investigation, unchanged)

- **Unknown-toolsets warning prefix** (`messaging`, `moa`) printed on every
  response — see `reticulum-bridge-issue-for-upstream.md`.
- **Clarify / accept-deny prompts** don't render over plain-text LXMF — same
  doc. These need a text-formatting + pending-prompt state layer in the
  adapter.

---

## E. Verification after any change

```
systemctl --user daemon-reload
systemctl --user restart hermes-reticulum
journalctl --user -u hermes-reticulum --since "30 sec ago" --no-pager | \
  grep -E "model pinned|session thread|Timeout|Bridge ready|Loaded existing"
```

Confirm the log shows the expected model pin, session thread, timeout, and
`Loaded existing identity` (not a new identity).
