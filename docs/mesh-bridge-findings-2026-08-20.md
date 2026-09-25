# Mesh Bridge — Operational Findings & Fix Log

_Durable record of what was diagnosed, fixed, and added to the
`rns_hermes_endpoint` mesh bridge in the 2026-08-20 session. Written so a
fresh Telegram session (or a future upstream PR) can pick up where we left
off without re-investigating._

Repo: `rns_hermes_endpoint` (local checkout)
Service: user systemd unit `hermes-reticulum.service` (editable venv install)
Profile: **default** (no `HERMES_PROFILE` in unit — intentional, per user)
Model: **pinned to a local GGUF** (switchable at runtime via `/model`; exact
name is deployment-specific and intentionally not recorded here)

---

## 1. The bridge "stopped replying" — root cause

The mesh/LXMF layer was never the problem. The bridge receives and ACL-gates
incoming messages fine; what failed was the one thing it shells out to for a
reply: `hermes chat -q`. Two compounding causes, both fixed:

### 1a. `hermes` not on the service `PATH` → "❌ Hermes Agent not found."
- The user systemd unit set no `PATH`, so the service inherited
  `PATH=/usr/local/bin:/usr/bin`.
- The `hermes` binary actually lives at `~/.local/bin/hermes` — **not** on
  that PATH.
- `find_hermes_bin()` returned `None`; every `chat -q` call failed at
  binary-lookup before ever reaching the model, and the client returned the
  canned `❌ Hermes Agent not found. Check your installation.`
- **Fix:** added to `~/.config/systemd/user/hermes-reticulum.service`:
  ```
  Environment=PATH=~/.local/bin:/usr/local/bin:/usr/bin
  ```
  then `systemctl --user daemon-reload && systemctl --user restart`.
- **Why it surfaced now:** the unit had no explicit `PATH`, so the service
  relied on whatever the user session exported. That's fragile — any change
  to the user's shell profile can silently break the service's ability to find
  `hermes`. Always pin `PATH` in the unit.

### 1b. Timeout was too tight (300s) and the model can be slow
- The bridge's `HermesClient` timeout was 300s; the endpoint-side limit the
  user saw was 600s. The 27b model, when driven through `chat -q`, can exceed
  300s even on a one-word message (reproduced: 27b hung past a 45s hard cap;
  the 35b answered the same `test` in well under 5s).
- **Fix:** `HERMES_TIMEOUT=600` in `.env`. `/new` added as a runtime escape
  hatch (see §3) so a wedged conversation can be reset without a service
  restart.

> Diagnostic note: a one-word "test" that times out is a **model
> responsiveness** signal, not a session-reuse problem. To confirm, time the
> exact bridge call directly:
> ```
> time hermes chat -q "test" --source reticulum -Q -m "<model>"
> ```
> If it hangs, the model/provider is the issue, not the bridge session logic.

---

## 2. Identity — it did NOT create a new one

The user reported "the endpoint made a new identity." Investigation:

- `~/.lxmf/storage/hermes_identity` mtime is **Jul 18** — untouched since
  creation (Aug 1 15:45 log: `Created new identity`; every start since:
  `Loaded existing identity`).
- The bridge announces a single LXMF address (a 64-hex pretty hash) — the
  **same** address used the whole time.
- The identity *file* on disk is 64 bytes (a 32-byte hash, no key). RNS
  prints two different "pretty" representations of the same identity:
  the file's short hash vs the long pretty form the bridge logs. **Same
  key, two renderings** — this is almost certainly what looked like a "new
  identity."
- **Action if it recurs:** dump the full identity hash and compare against
  the client (run from the project venv):
  ```
  python -c "import RNS; i=RNS.Identity.from_file('<STORAGE_DIR>/hermes_identity'); print(RNS.prettyhexrep(i.hash))"
  ```
  where `<STORAGE_DIR>` is the LXMF storage dir (default `~/.lxmf/storage`).

---

## 3. New features added this session (in `src/hermes_reticulum/core/`)

All wired through an **extensible command dispatcher** — adding a new `/`
command is now one entry in the `COMMANDS` dict in `commands.py`, no `cli.py`
changes.

| File | Purpose |
|------|---------|
| `commands.py` | `CommandDispatcher` + `COMMANDS` registry. Routes `/model`, `/new`, `/help`, `/commands`. Returns `None` for non-commands so they fall through to the model. |
| `model_command.py` | `/model` handler — lists available models, switches, resets, persists pin to `~/.lxmf/state/model.json`. |
| `hermes_client.py` | `HermesClient` now: pins a model (`-m <model>`, from `HERMES_MODEL` env or runtime `set_model()`); manages a named session thread (`mesh-<source>`) for continuity; `reset_session()` bumps the thread name for `/new`. |

### Commands available on the mesh
- `/model` — list models + active; `/model <name>` to switch; `/model reset`
- `/new` — start a fresh conversation (drops context) — the escape hatch for
  a wedged 27b conversation
- `/help` / `/commands` — list all commands the endpoint understands

### Session continuity change (needs user awareness)
Mesh messages now **resume a named thread** (`mesh-reticulum`) via
`-c <name> --create-if-missing` instead of each call being isolated. This is
what makes `/new` meaningful. If per-message isolation is preferred instead,
remove the `-c ... --create-if-missing` branch in `hermes_client.chat()`.

### `.env` keys now used
- `HERMES_MODEL=<pinned local model>` (deployment-specific; not recorded here)
- `HERMES_TIMEOUT=600`
- (existing) identity, stamp cost, display name, ACL allowlist (2 users:
  mobile + desktop)

---

## 4. Access control — confirmed locked to user identities

- ACL is in **allowlist mode** (`allow_all=False`, 2 allowed, 0 blocked).
- The two allowed hashes (mobile + desktop) live in the `.env`
  `HERMES_RETICULUM_ALLOWED_USERS` — do **not** echo values.
- Anyone else who finds the endpoint on the mesh gets
  `⛔ Access not authorized.` and never reaches Hermes.

---

## 5. Announce cadence — RNS defaults, not configured

No announce interval is set anywhere (no `reticulum.yaml` in the default
location). RNS built-in defaults govern re-announcement:
- Announce **check** interval: `1s` (how often RNS scans for due announces)
- **Management** announce interval: `2h` (re-assert cadence)

So the endpoint announces on startup and re-asserts roughly every 2 hours. To
make it re-announce more aggressively (flaky-mesh use), that would be a change
in the bridge's announce loop — RNS does not expose it as a plain config knob.

---

## 6. Still-open / worth an upstream PR

1. **`PATH` fragility in user systemd units** — the "not found" bug came from
   inheriting the user session's `PATH`. Consider: the bridge should fail
   loudly at startup if `find_hermes_bin()` returns `None` (it currently
   raises `RuntimeError`, which is good — but the service should log a
   clear, actionable message and the unit should pin `PATH`).
2. **Timeout vs model responsiveness** — a 600s cap hides a wedged model.
   Consider a shorter *first-token* timeout or a liveness probe so a hung
   model is detected quickly rather than after the full cap.
3. **The older upstream issues** (see `reticulum-bridge-findings.md` /
   `reticulum-bridge-issue-for-upstream.md`): unknown-toolsets warning
   prefix (`messaging`, `moa`), and clarify/accept-deny prompts not rendering
   over plain-text LXMF. Still open, independent of this session's fixes.

---

## 7. How to re-verify after any change

```
# editable install → src/ changes are live; just restart
systemctl --user daemon-reload
systemctl --user restart hermes-reticulum

# confirm model pin + session thread + identity in the log
journalctl --user -u hermes-reticulum --since "30 sec ago" --no-pager | \
  grep -E "model pinned|session thread|Timeout|Bridge ready|Loaded existing"

# confirm hermes resolves on the service PATH
tr '\0' '\n' < /proc/$(pgrep -f "hermes-reticulum run")/environ | grep '^PATH='
```
