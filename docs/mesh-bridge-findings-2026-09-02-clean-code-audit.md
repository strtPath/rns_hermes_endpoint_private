# Clean Code Audit — 2026-09-02

Scope: all of `src/hermes_reticulum/` + `tests/`. Method: 4 parallel subagent audits (one per file group) + flake8. Tests pass 74/74 at audit time.

## Bugs found (fix before upstream PR)

1. **commands.py L196-197** — `_cmd_verbose`: `verbose` is a `bool`, compared to `""`. The "show current state" branch is dead code; `/verbose` with no args silently sets verbose=False instead of reporting state.
2. **profiler.py L78/82/86** — three consecutive `except Exception: pass` around RNS Transport calls swallow *all* errors including MemoryError/KeyboardInterrupt, and hide real API drift (AttributeError).
3. **plugin/registration.py L29** — `validate_config()` unconditionally returns True; docstring claims it validates. Dead/misleading.

## High-priority structural issues

| File | Issue |
|------|-------|
| cli.py L83-246 | `cmd_run` is 163 lines: inits 5 components, defines 4 inner callbacks, wires them all. Extract `_init_components()` + `_wire_callbacks()`. |
| hermes_client.py L806-973 | `_run_with_liveness_guard` 168 lines — subprocess mgmt + retry + error handling + watcher orchestration in one function. |
| hermes_client.py L643-804 | `chat()` 162 lines; builds the same `cmd` list twice (L675-692 and L755-765) — extract a `_build_cmd()` helper. |
| control_server.py L302-399 | `_handle_post()` 97-line if/elif router over 9 endpoints → dispatch table or per-endpoint methods. |
| bridge.py L338-412 | `send_reply()` 74 lines: identity resolution (8×retry) + destination build + LXMF dispatch → extract `_resolve_identity()`. |
| commands.py L330-384 | `handle()` has a 30-line alias if/elif chain → dict mapping. |

## Duplicated logic (extract helpers)

- **hermes_client.py**: state.db open/query pattern repeated in 4 functions (`_run_step_watcher`, `_resolve_session_id`, `tool_recap`, `session_token_stats`) — no shared helper; `"~/.hermes/state.db"` default appears 4×.
- **commands.py**: `_ctrl(ctx)` + session-validation prologue repeated identically in `_cmd_approve`/`_cmd_deny`/`_cmd_steer`/`_cmd_tools`; approve/deny are near-clones; `from pathlib import Path` imported locally 3× inside `_cmd_steps`.
- **cli.py**: "no mesh peer" guard + push/log pattern triplicated in `_on_tool_step`/`_on_gate_open`/`_on_full_step`; `push["hash"][:16]` 5×.
- **acl.py L57/L75**: hash normalization (`lower().replace(" ","").replace(":","")`) duplicated — extract `_normalize_hash()`.

## Overly broad exception handling (no `# noqa: BLE001` justification)

- hermes_client.py: 7× bare `except Exception` in `chat`, `_run_with_liveness_guard`, `_with_tool_recap`, `_kill_process`, `pause`, `resume`, `version`.
- control_server.py L220/262/446; bridge.py L316/332/407/427 (`_clean_exit` is a bare-except → `os._exit(0)` nuclear bailout with no log); cli.py L221/315; plugin/adapter.py L100.

## Magic numbers (name them)

- Hash display truncation: `[:16]` in bridge/cli/profiler, `[:12]` in commands — one shared constant per file or a module-level `HASH_DISPLAY_CHARS`.
- hermes_client.py: hold-gate `1800`s (L486), retry sleep `5`s (L751), stderr truncations 200/500, recap limit inconsistency (`tool_recap(limit=10)` default vs hardcoded `limit=8` at L990).
- control_server.py: token bytes `32`, queue maxsize `256`, join timeout `5`.
- bridge.py: path-request retries `8` × sleep `1`s, chunk delay `0.5`s.
- profiler.py: MTU threshold `300`, hop threshold `3`, UTF-8 multiplier `3`.
- core/adapter.py: boundary ratios `0.5/0.4/0.3` repeated across two functions.

## Dead code / unused

- control_server.py L9: `import socket` unused; `/stop` POST endpoint is a no-op (L378).
- profiler.py: constants `TCP_MIN_BITRATE`, `RSSI_FAIR`, `SNR_GOOD` defined, never referenced.
- plugin/adapter.py: `import asyncio` unused + `_loop` attribute declared but never assigned.
- hermes_client.py L1090: f-string with no placeholders (F541).
- commands.py: `_cmd_stop`/`_cmd_new` accept `args` and ignore it; `_cmd_tools` returns "No tool events recorded yet." when the control server is down — misleading message.

## Naming / docstring gaps

- hermes_client.py: `_diag`, `sid`, `cname/cargs/cid`, `rid`, `gen` (generation counter, never explained), `_last_run_ms` attached via getattr and never declared in `__init__`.
- cli.py L48: `_deny_veto` *executes* the veto — name suggests it denies.
- control_server.py: 15 public methods without docstrings incl. `_handle_post()` (the central router); `classify_tool()` docstring omits that `browser_*`/`execute_*` prefixes are auto-risky.
- cli.py/model_command.py: inner helpers (`add`, `m`, `fh`, `low`) cryptic; several public methods lack Args sections.

## Test issues

- test_profiler.py: all 4 classes use bare `assert` and don't inherit `unittest.TestCase` (pytest-only); `test_no_metrics_fallback` asserts membership in a 2-value tuple — non-deterministic.
- test_control_server.py: reaches into `state._pending`/`_decisions` private internals; `[IP_ADDRESS]` bind pattern is platform-specific.
- test_turn_alive.py: hook tests dynamically import from `~/.hermes/hooks/`, patch module globals, and poll with a sleep loop — brittle to any hook change.

## Flake8 (project venv, max-line 100)

50 findings total; the substantive ones are listed above (F401/F841/F541). The rest are E303/E306/W293 whitespace noise — run `black` or a flake8 auto-fix pass before PR.

## Suggested fix order for pre-PR cleanup

1. Fix the 3 bugs (verbose dead branch, profiler silent excepts, validate_config).
2. Extract helpers: `_build_cmd()` in hermes_client, dispatch table in control_server, `_resolve_identity()` in bridge, alias dict in commands, `_normalize_hash()` in acl.
3. Name the magic numbers; delete dead imports/constants.
4. Split `cmd_run` and `_run_with_liveness_guard`.
5. Whitespace pass + flake8 clean.
