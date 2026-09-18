# Mesh-tool-gate Jev triage layer — implementation plan

**Status:** proposed · **Owner:** Holo · **Spec src:** `docs/mesh-bridge-findings-2026-09-17-jev-systemone-integration.md` §4

## Goal

Add a confidence-gated Jev "fast path" in front of the mesh-tool-gate's human
approve/deny, so the operator doesn't babysit every benign mesh command but
still personally reviews every critical/destructive one. Toggleable, safe by
default, fail-open.

## Verified facts (this session)

- `jev-skill-suggest` plugin WORKS. Logs show LOAD mode firing on
  `qwen3.8-27b-iq3_xxs` (00:52 p=0.59, 01:00 p=0.74 → full skill body injected)
  and HINT mode on deepseek-v4-flash (01:22 p=0.69 → this very turn).
- The repo gate source and the live plugin are byte-identical
  (`src/hermes_reticulum/mesh-tool-gate/__init__.py` == `~/.hermes/plugins/mesh-tool-gate/__init__.py`).
- Jev key source: `gateway/run.py::load_hermes_dotenv()` loads `~/.hermes/.env`
  into the gateway process env at startup, so a plugin's `os.environ[...]`
  key read works. `OPENROUTER_API_KEY` is present in `.env` (1 non-commented
  occurrence). The triage layer can reuse the SAME mechanism as `jev-skill-suggest`.
- Jev endpoint (live-probed): `POST https://openrouter.ai/api/alpha/decisions`,
  model `typesafe/jev-1.13`. Working client: `docs/probe_jev.py`.

## Design

### Toggle (config, NOT .env — per hermes-agent AGENTS.md: .env is secrets only)

Non-secret behavioral settings belong in `config.yaml`. Add a `plugins.mesh_tool_gate`
block (or reuse the existing env-style the plugin already uses, since the plugin
predates the rule — **decision needed**, see below):

```yaml
plugins:
  mesh_tool_gate:
    triage: auto        # off | allow_benign | hint_only | auto
    triage_conf_floor: 0.6      # universal confidence floor
    triage_stakes_gate: true    # never auto-allow ruthless/destructive
```

Values:
- `off` (default) — gate behaves exactly as today. Zero change for users who don't opt in.
- `hint_only` — Jev classifies and LOGS a hint, but never changes gate outcome
  (dry-run; tune thresholds on real traffic before enabling `allow_benign`).
- `allow_benign` — auto-allow only when ALL of: `handling==allow`, `risk < 0.2`,
  `stakes ∈ {routine, sensitive}`, `confidence >= floor`. Anything else escalates.
- `auto` — reserved for a future stakes-aware mode that also auto-denies proven
  malicious-to-the-mesh calls; NOT in the first cut.

### Placement

In `src/hermes_reticulum/mesh-tool-gate/__init__.py`, inside `on_pre_tool_call`,
immediately before the `_gate_tool(...)` call for each dangerous/execute_code
path. A new `_jev_triage(command, desc, tool_name) -> "allow" | "escalate" |
"deny" | "error"` helper; `error` and `deny` never proceed.

### Routing (deterministic code, per TypeSafe "keep code in control")

```
jev = _jev_triage(...)
if jev == "allow" and _triage_mode() == "allow_benign":
    log; return None            # proceed, skip human gate
if jev == "escalate" or jev == "error":
    return _gate_tool(...)      # existing human approve/deny (fail-closed)
if jev == "deny":
    return _block_message(...)  # existing block wording
```

### Safety invariants (hold these)

- **Fail-open** on any Jev error / timeout / missing key / low confidence:
  always escalates to the human gate. Never auto-allow on uncertainty.
- **Hardline floor untouched:** `detect_hardline_command` blocks outright BEFORE
  Jev ever sees the call. Jev never sees (and never gates) no-recovery commands.
- **execute_code always gates** unless the operator sets `allow_benign` AND Jev
  is very confident (decision below). Python payload is opaque — the safe default
  is human review.
- **No sensitive data in Jev `state`:** state carries only tool name + a truncated
  command line + tool class. No session PII, no full file contents (the bridge
  already quarantines PII — keep consistent).

## Open decisions (need Hamza's call)

1. **Toggle location:** live plugin already uses env vars (`MESH_GATE_TIMEOUT`,
   `HERMES_YOLO_MODE`, `HERMES_MESH_PROFILE`). hermes-agent's rule says non-secret
   settings go in `config.yaml`, but this plugin predates that rule and its whole
   config surface is env. Options: (a) stay env-consistent (`MESH_GATE_TRIAGE`),
   (b) read config.yaml `plugins.mesh_tool_gate.triage`. env is simpler + matches
   the plugin; config.yaml is the "right" home. Recommend (a) for consistency,
   accept (b) if users say "no more env vars."

2. **Is auto-allow of benign commands acceptable at all?** The user said "still
   forward SOME commands to the user to choose, but only the most critical ones."
   This is the consequential design line. Confirm intent: fast-path the routine
   `git status`/`ls`/read-type terminal calls without a human, and only escalate
   destructive/privileged/opaque/PII-touching ones.

3. **execute_code:** keep it always-human (safe default) or let Jev auto-allow it
   at high confidence? Recommend always-human for the first cut.

4. **Confidence floor:** propose `0.6` universal, and NEVER auto-allow when
   `stakes==destructive`. Tune with real gate history once `hint_only` produces it.

5. **First cut scope:** ship `hint_only` first (dry-run, no behavior change) → let
   real traffic calibrate → then flip to `allow_benign`. Or go straight to
   `allow_benign` with conservative thresholds. Recommend the dry-run first.

## Deliverables

- `src/hermes_reticulum/mesh-tool-gate/__init__.py` — triage helper + toggle wiring
- `docs/mesh-bridge-findings-2026-09-18-jev-gate-triage.md` — this plan + verification
- tests: `tests/test_mesh_tool_gate.py` additions (routing table: allow/escalate/deny/error)
- `.env.example` / README line documenting the toggle

## Verify (before calling done)

1. `venv/bin/python -m pytest tests/ -x -q` (full suite green)
2. With triage off: gate behavior identical (diff the two code paths).
3. With `hint_only`: a benign mesh terminal call logs a Jev hint + escalates to human gate.
4. With `allow_benign`: a routine call proceeds WITHOUT a `/approve` push; a
   destructive call still pushes "⚠️ PRE-EXEC APPROVAL" to the mesh.
5. Any Jev error (kill key) → all gate calls escalate to human (fail-open proven).