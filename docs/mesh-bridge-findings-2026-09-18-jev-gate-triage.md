# Jev pre-gate triage for mesh-tool-gate — implementation + live findings

**Status:** implemented, unit-tested (29 new), live-parser-probed. Not yet
gateway-enabled (restart required; default is `off`).

**Spec:** `docs/mesh-bridge-findings-2026-09-17-jev-systemone-integration.md` §4
**Plan:** `docs/plan-2026-09-18-jev-gate-triage.md`
**Tests:** `tests/test_mesh_tool_gate_triage.py`, `tests/test_mesh_tool_gate_triage_wiring.py`

## What this is

A confidence-gated Jev "fast path" in front of the mesh-tool-gate's human
approve/deny. When enabled (`allow_benign`), Jev rates a gated tool call and
may **auto-allow** ROUTINE calls (e.g. a `git status` a human would clear
without thinking), skipping the 900s `/approve|/deny` wait. Anything
sensitive, destructive, low-confidence, or a Jev error still escalates to the
existing human gate (fail-closed). The human stays the backstop for critical
actions.

## Design decisions (Hamza, 2026-09-18)

1. Jev **may** auto-allow benign mesh commands; the human still gates critical
   ones.
2. First cut = **dry-run `hint_only`** (Jev rates + logs, never changes gate
   outcome) → then flip to `allow_benign`.
3. `execute_code` **may** be auto-allowed by Jev at high confidence
   (`MESH_GATE_TRIAGE_EXEC=1`); default `0` keeps it always-gated.
4. Toggle is **env-consistent** with the existing plugin surface
   (`MESH_GATE_TRIAGE`), not config.yaml (the plugin's whole config is env;
   see the pending config-home decision below).

## Toggle (env, read at plugin import — gateway restart needed)

```
MESH_GATE_TRIAGE=off        # default — gate works exactly as before
MESH_GATE_TRIAGE=hint_only  # dry-run: Jev rates + logs, gate outcome unchanged
MESH_GATE_TRIAGE=allow_benign   # Jev may auto-allow ROUTINE calls
MESH_GATE_TRIAGE_CONF=0.6   # confidence floor for auto-allow (0..1)
MESH_GATE_TRIAGE_EXEC=0     # 1 = let Jev auto-allow execute_code at high conf
```

`off` **short-circuits before any Jev call** (proven by test) — enabling the
feature costs one outbound HTTP call per gated tool; disabling it costs
nothing.

## Layout

- `src/hermes_reticulum/mesh-tool-gate/triage.py` — **pure** Jev decision
  logic (no Hermes deps), importable in the bridge venv for unit tests.
  `verdict()` routes `(handling, risk, stakes, confidence)` →
  `allow | escalate | deny`.
- `src/hermes_reticulum/mesh-tool-gate/__init__.py` — Jev client
  (`_ask_jev`), state builder (`_triage_state`), and wiring
  (`_triage_tool_call`) invoked in `on_pre_tool_call` for the `execute_code`
  and dangerous-terminal paths.
- `install.sh` copies the whole plugin dir (`cp -r`), so `triage.py` rides
  along to `~/.hermes/plugins/mesh-tool-gate/`; the loader imports the plugin
  as `hermes_plugins.mesh-tool-gate` with `__path__` set, so the relative
  `from . import triage` resolves.

## Safety invariants (held, tested)

- **`off` → escalate with zero Jev calls** (`test_off_short_circuits...`).
- **`hint_only` → Jev runs but verdict is forced to escalate**
  (`test_hint_only_runs_jev_but_escalates`): dry-run never changes the gate.
- **fail-open** on any Jev error / missing key / timeout → escalate
  (human gate backstop). `test_allow_benign_jev_error_escalates`,
  `test_allow_benign_missing_key_escalates`.
- **Auto-allow only ROUTINE**, `conf >= floor`, `risk < 0.2`. `sensitive` and
  `destructive` NEVER auto-allow (sensitive → escalate; destructive →
  escalate or deny). `risk >= 0.2` → escalate.
- **deny** only when Jev says `deny` OR destructive + `risk > 0.8`.
- **hardline floor untouched**: `detect_hardline_command` blocks outright
  before Jev ever sees the call. Jev is never the decider for a no-recovery
  command.
- Jev `state` is minimal + non-PII (tool name, tool class, truncated command
  line ≤ 400 chars) — consistent with the bridge's PII quarantine rule.

## Live findings — Jev's REAL answer shape (probed 2026-09-18)

The §4 spec assumed `{risk, stakes, handling, confidence}` with a clean
`stakes` bucket. Live Jev (OpenRouter `typesafe/jev-1.13`, `/api/alpha/
decisions`) returns:

- `risk`: `{"noul": <0..1>}` — handled by `_noul`.
- `stakes`: a **score-object**, e.g.
  `{"type":"score","score":0.44,"legend":{"0":"routine","1":"sensitive",
  "2":"destructive"},"probabilities":{"0":0.58,"1":0.41,"2":0.01},
  "confidence":0.34}`. The per-bucket `probabilities` are Jev's own best read
  of the winner → **argmax over the legend** is more faithful than
  thresholding the raw `score`. (`score:0.44` would mislabel as "sensitive"
  even though Jev says routine 0.58.)
- `handling`: `{"choice":"allow|escalate|deny", "confidence":<0..1>}`.
- **Two confidences** — the handling-choice's and the stakes-score's. The
  gate uses `confidence = min(handling_conf, stakes_conf)`: a call rated
  destructive-but-unconfident must still reach a human, and so must a
  low-stakes-unconfident one. Strongest possible conservatism for a
  security control.

### Calibration observation (why dry-run first was right)

Live routing with `conf_floor = 0.6`:

| call | stakes | conf | risk | routing |
|---|---|---|---|---|
| `git status` | routine | 0.39 | 0.11 | **escalate** |
| `cat ~/notes.md` | sensitive | 0.31 | 0.14 | **escalate** |
| `rm -rf /` | destructive | 0.99 | 0.97 | **deny** |

Jev rates even `git status` at ~0.39 confidence, so with a 0.6 floor **nothing
routinely auto-allows yet** — the gate behaves like today. That's the honest
state of the dry-run: conservative by design, safe. Options before flipping
to a live `allow_benign`:

1. **Lower `MESH_GATE_TRIAGE_CONF`** (e.g. 0.3) so high-probability routine
   calls (git status: routine 0.58) qualify. Only after `hint_only` logs show
   accurate auto-allow decisions at the chosen floor.
2. Keep 0.6 and accept Jev rarely auto-allows — the feature mostly disables
   the 900s wait for the *clearest* routine calls.
3. Tune `TRIAGE_CONF_FLOOR` per-tool if a tool's Jev confidence is
   systematically low.

Recommendation: run `hint_only` on real mesh traffic for a few days, collect
the `Jev triage ... -> ...` lines from `agent.log`, and set the floor from
that distribution — don't pick it by sight on 3 calls.

## Verification steps

- `./venv/bin/python -m pytest tests/test_mesh_tool_gate_triage.py tests/test_mesh_tool_gate_triage_wiring.py -q` → 29 passed.
- Full suite: `./venv/bin/python -m pytest tests/ -q` → 218 passed.
- Repo and live plugin are byte-identical
  (`src/hermes_reticulum/mesh-tool-gate/*` == `~/.hermes/plugins/mesh-tool-gate/*`).
- Live probe (real Jev + real key, from the live plugin): git status →
  escalate, cat → escalate, rm -rf / → deny.

## Live-enable (operator)

The running gateway loaded the pre-triage plugin code at 00:51. To live-enable:

1. Edit `~/.hermes/.env`: uncomment/set
   `MESH_GATE_TRIAGE=hint_only` (start dry-run), optionally
   `MESH_GATE_TRIAGE_CONF` / `MESH_GATE_TRIAGE_EXEC`.
2. **Restart the gateway** (plugin source is read at import). See the
   stop/restart pitfall in `rns-hermes-endpoint` skill (SIGTERM now unwinds
   cleanly via `_clean_exit`).
3. Send a mesh tool-calling message; grep
   `mesh-tool-gate: Jev triage` in `agent.log` (or journald).
4. `hint_only` logs show `(return='escalate')` — outcome never changes.
5. Flip `MESH_GATE_TRIAGE=allow_benign` when the logged classifications look
   right, and confirm a ROUTINE call no longer pushes "⚠️ PRE-EXEC APPROVAL".

## Open

- **config-home decision (pending).** hermes-agent AGENTS.md says non-secret
  settings belong in config.yaml, not `.env`; the plugin's whole existing
  surface (`MESH_GATE_TIMEOUT` etc.) is env. Hamza chose env for the toggle
  for consistency. Revisit if a future cleanup moves the plugin to config.yaml.
- **Threshold tuning** needs real `hint_only` traffic (above).
- **Auto-**deny mode (`auto` in the old plan) not implemented — reserved.