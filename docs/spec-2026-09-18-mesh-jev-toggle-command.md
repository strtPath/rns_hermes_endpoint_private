# Spec: `/jev on|off` — runtime toggle for Jev pre-gate triage

Status: DRAFT

## 1. Problem (user perspective)

The user works through the Reticulum mesh while away from the screen. Step-through
mode is for watching; the Jev triage stage (MESH_GATE_TRIAGE, env) is for NOT
watching — auto-clearing routine tool calls so no /approve prompt appears.

Today the triage mode is fixed at gateway import: the only way to change it is to
edit ~/.hermes/.env and restart the gateway. The user wants a mesh command to
switch the triage on or off at runtime, per session, without a restart.

Intended behaviour in one sentence: `/jev on` enables Jev triage for this mesh
session (Jev may auto-allow routine calls), `/jev off` disables it (every risky
tool goes to the human gate as before), `/jev` reports the current mode.

## 2. Root cause / gap (diagnosis)

- `~/.hermes/plugins/mesh-tool-gate/__init__.py:117` — `TRIAGE_MODE` is read once
  at plugin import from `os.environ.get("MESH_GATE_TRIAGE", "off")`. It is a
  module-level constant; `_triage_tool_call()` (line 498) reads it directly, so
  nothing can change it at runtime.
- The plugin runs in the gateway process; the bridge (where mesh slash commands
  are handled) runs in a separate process. The bridge has no channel to the
  plugin's memory.
- Precedent: step-through mode solves exactly this cross-process problem via a
  state file. `MODE_STATE_PATH` (`~/.hermes/.reticulum-step-mode`) is written by
  the bridge (`_cmd_steps`, commands.py:289) and read on each agent:step event by
  the gateway hook (`~/.hermes/hooks/mesh-tool-events/handler.py`,
  `HERMES_STEP_MODE_FILE`).

## 3. User stories

- US-1 (core): As a mesh operator at work, I want `/jev on`, so routine gated
  tool calls are auto-cleared by Jev and I don't get an /approve prompt.
- US-2 (core): As the same operator, I want `/jev off`, so Jev stops deciding and
  every risky tool waits for my explicit /approve — e.g. when I am back at the
  screen and want to watch every step.
- US-3 (reporting): `/jev` with no argument reports the effective mode for this
  session and the global env default.
- US-4 (edge): With no explicit session state, the command reports the env value
  (from ~/.hermes/.env) and the effective mode is that value — no file write
  needed.
- US-5 (edge): `/jev` and `/jev on|off` are distinct from `/steps`, `/approve`,
  `/deny`; an unknown argument returns usage, not a model passthrough.
- US-6 (scope boundary): Toggling triage does NOT bypass the hardline
  no-recovery block and does NOT affect hint_only vs allow_benign — if the env
  mode is hint_only, `/jev on` still only logs, never auto-allows.
- US-7 (multi-session): A `/jev on` in one mesh session does not change another
  session's effective mode.

## 4. Test seams

- `tests/test_commands.py` — add `/jev` cases alongside the existing slash-command
  handlers (fake `CommandContext`, no control server needed).
- New plugin-level test (unit, no live Jev): `_triage_tool_call` reads the
  session state file before `TRIAGE_MODE` — fake the file, fake `_ask_jev`,
  assert routing for off/env-default/session-on/hint_only.
- Sibling invariants that must stay green: existing `/steps`, `/hold`, `/go`
  tests; hardline block tests (detect_hardline_command fires before triage);
  all 29 triage tests on the feat/jev-gate-triage branch.

## 5. Implementation decisions

Invariants:
- `off` short-circuits before any Jev call (zero HTTP).
- `hint_only` never changes the gate outcome.
- Fail-open on any Jev error: escalate to the human gate.
- Hardline block precedes triage always.
- Session state wins over env when present.

Changes:

1. State file (new, mirroring MODE_STATE_PATH):
   - Path constant in `bridge.py`: `TRIAGE_STATE_PATH =
     os.environ.get("HERMES_JEV_STATE_FILE",
     os.path.expanduser("~/.hermes/.reticulum-jev-state"))`.
   - Content: `off`, `on`, or `<env>` (meaning "inherit env"). Written by the
     bridge; read by the gateway plugin per triage call.
2. Plugin (`~/.hermes/plugins/mesh-tool-gate/__init__.py` + repo copy on
   feat/jev-gate-triage): in `_triage_tool_call`, resolve effective mode as
   session-state-file value if present, else `TRIAGE_MODE` (env). Pass the
   effective mode into `triage.verdict()` instead of the module constant. The
   conf floor and EXEC toggle stay env-fixed for now (out of scope).
3. Bridge command (`src/hermes_reticulum/core/commands.py`):
   - `_cmd_jev(ctx, args)`:
     - `on`  -> write `on` to state file, reply confirming Jev triage ON
       (auto-allow routine; sensitive/destructive still human-gated).
     - `off` -> write `off`, reply confirming OFF (human gate on all risky tools).
     - no arg -> report effective mode (file value if set, else env value) and
       usage line.
     - other -> usage line.
   - Register `"/jev": _cmd_jev` in COMMANDS. No alias branch (word `jev` is not
     ambiguous with existing aliases).
4. The state file is the source of truth (same pattern as step mode): the plugin
   re-reads it on every triage call; no gateway restart needed.

Explicitly NOT in scope:
- Per-session confidence floor (MESH_GATE_TRIAGE_CONF stays env/global).
- Toggling hint_only vs allow_benign from the mesh (env decides).
- Persisting `/jev` state per named session in a DB (single global file is
  enough while there is one operator; multi-session isolation in US-7 is
  satisfied by the file being per-operator, not per-session — if that
  requirement is rejected, the file gains a session-keyed JSON body).
- Merging feat/jev-gate-triage into dev (branch policy: user decides).

## 6. Testing decisions

- Runner: `./venv/bin/python -m pytest tests/ -q` in the repo (editable install;
  tests run against repo source, live plugin is re-copied via install.sh on
  deploy).
- New: `tests/test_jev_command.py` — `_cmd_jev` on/off/report/usage against a
  temp state file (monkeypatch the path constant).
- New: plugin wiring test that `_triage_tool_call` consults the state file
  (file present beats env; absent file falls through to env; `off` short-
  circuits with zero `_ask_jev` calls; `hint_only` from env still escalates even
  when session file says `on`).
- Passing = full suite green + live check: `/jev on` on the mesh, send a
  tool-calling message, grep `Jev triage` in agent.log for the new effective-mode
  log line; `/jev off` then no Jev triage lines at all.

## Feedback loop (red before fix)

`tests/test_jev_command.py::test_jev_off_disables_env_on` — with env
MESH_GATE_TRIAGE=allow_benign and state file `off`, a gated tool call must
escalate without any Jev HTTP call. Fails today because no `/jev` command
exists and the plugin ignores any state file.
