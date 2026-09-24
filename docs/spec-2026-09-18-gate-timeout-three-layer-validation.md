# Spec: startup validation of the full three-layer gate-timeout ordering

**Status:** BACK BURNER (not implemented). Spec'd 2026-09-18.
**Related:** `docs/mesh-bridge-findings-2026-09-18-unanswered-gate-wedges-turn.md`,
`docs/pre-tool-callback-timeout-issue.md`.

## Why this exists beyond options 1 & 2

Options 1 and 2 already land:
- **Option 1** (`_validate_gate_timeouts`, shipped): refuses to start when the
  two *bridge-side* timeouts (`MESH_GATE_TIMEOUT` vs
  `HERMES_MESH_APPROVAL_TIMEOUT`) are mis-ordered. Both live in the repo's
  `.env` and are readable in the bridge process.
- **Option 2** (README): documents that all three must be set and their exact
  ordering.

The **gap** is the *third layer*, `plugins.hook_callback_timeout`, which the
bridge cannot see. Two real failure modes slip past options 1 & 2:

1. **Hermes changes its default across versions.** v0.19.0 (per old README)
   had no `hook_callback_timeout` key at all; v0.21.x ships it at 30s. A user
   deploying the bridge pins *their* Hermes build and its behavior moves
   underneath them — a 900/900 bridge config is safe only if their Hermes
   build exposes a hook timeout they've raised above 900 (impossible, 600
   clamp) or that key doesn't exist (v0.19-style direct block).
2. **The user overrides it themselves.** `hook_callback_timeout` is just a
   `config.yaml` key; power users will tune it, possibly to a value below the
   gate timeouts (recreating the wedge) or above 600 (silently clamped).

Option 1 is blind to both because it only reads the two repo-controlled env
vars. The fix for *unknown* third-layer config is to read the **effective**
Hermes value at startup and validate the full ordering.

## Design

**Where:** bridge startup, in `_validate_gate_timeouts` (or a sibling), after
`_load_dotenv()`.

**What it reads:**
- `MESH_GATE_TIMEOUT` and `HERMES_MESH_APPROVAL_TIMEOUT` from the bridge env
  (already read).
- The **effective** `hook_callback_timeout` from Hermes. Two options:
  - (a) Parse `~/.hermes/config.yaml` directly for
    `plugins.hook_callback_timeout`; fall back to the Hermes-core default
    (30s) if the key is absent.
  - (b) Shell out to `hermes config get plugins` (the README already tells
    users to run this). More robust to Hermes schema drift, but adds a
    subprocess + assumes `hermes` on PATH.

  **Recommend (a) first** — synchronous, no subprocess, and the absence of the
  key is itself informative (v0.19 vs v0.21 distinction). (b) only if (a)
  proves brittle to Hermes config-schema changes.

**Validation rule (full ordering):**
`HERMES_MESH_APPROVAL_TIMEOUT <= MESH_GATE_TIMEOUT < hook_callback_timeout < 600`.

- If `MESH_GATE_TIMEOUT < HERMES_MESH_APPROVAL_TIMEOUT` → refuse to start
  (already covered by option 1).
- If `hook_callback_timeout` is absent/unknown → do NOT hard-fail; log a loud
  WARNING telling the operator to verify their Hermes build's hook timeout and
  set it above `MESH_GATE_TIMEOUT`. This is the v0.19 vs v0.21 ambiguity — a
  hard fail would break legit older builds that have no such key.
- If `hook_callback_timeout <= MESH_GATE_TIMEOUT` → refuse to start with a
  corrective message (the wrapper would abandon the plugin before the gate
  resolves → the documented wedge).
- If `hook_callback_timeout >= 600` → warn loudly (Hermes clamps to 600
  silently; the reported value is not what runs).

## Delta vs option 1

- Uses the **same startup choke point** and `sys.exit(1)` pattern.
- Adds one config-key read (Hermes) so the check is against the *effective*
  runtime value, not just the repo's own env.
- The "key absent" case must be a warning, not a hard fail, to stay compatible
  with Hermes versions that predate the key.

## Fatal vs warning policy (user-stated: validate/reject, never silently clamp)

- Definite misconfiguration (`hook <= gate`) → `sys.exit(1)`, refuse to start.
- Ambiguity (`hook` key absent) → loud WARNING + continue (a hard exit would
  break legitimate older deployments).
- Out-of-clamp (`hook >= 600`) → loud WARNING (Hermes clamps silently; we
  surface the discrepancy, don't pretend the configured value runs).

## Test

Extend `tests/test_gate_timeout_validation.py` with cases for: hook absent
(passes with warning), hook > gate (passes), hook <= gate (exits), hook >= 600
(warns). Mock the config read. Pin the contract, prove red on the pre-fix
path.

## Triggers for promoting off the back burner

- A user report of the wedge on a Hermes build where options 1 & 2 were in
  place (i.e. defaults changed under them).
- The README's `hermes config get plugins` verify step stops matching reality
  for a fresh deployer.
- Multiple requests where the shipped 900/900/30 defaults bit someone who
  missed the README.

## Out of scope / rejected

- **Hard-coding 480/480/490 as defaults.** Removes operator flexibility (LoRa
  needs a big window) and can't be reconciled with the 600 clamp. Not a default
  value change; validation is the right mechanism.
- **Reading the third layer from within the plugin (gateway process).** The
  plugin doesn't know the bridge's two values; the bridge is the correct home.