# Findings: mesh tool glyphs did not match the Telegram gateway

Date: 2026-09-15
Session: emoji parity pass

## Observation

Every tool the mesh showed carried the same glyph. Three separate code paths
each hardcoded one:

- `cli.py::_on_tool_step` pushed `f"🔧 {label}"` for every `agent:step` hook event.
- `control_server.py::ToolStep.summary()` built `f"🔧 {self.name}"`.
- `hermes_client.py::_push_step` (the CLI-side watcher, which is the path that
  actually delivers per-tool messages for a mesh turn) used `💻` for every tool
  and `❌` on error.
- `hermes_client.py`'s recap footer used a bare `🔧 ` plus tool names.

So `read_file`, `terminal`, `web_search` and `execute_code` all rendered
identically, and a failed call was distinguishable only by the `❌` swap. The
Telegram gateway meanwhile renders each tool with its own emoji
(`📖 read_file`, `💻 terminal`, `🔍 web_search`, `🐍 execute_code` …) via
`agent/display.py::get_tool_emoji`, which resolves skin override → tool registry
→ default.

Two sessions describing the same work therefore produced visibly different
transcripts depending on which platform the operator was on — the mesh looked
like a different (and less legible) agent.

## What was done

New module `src/hermes_reticulum/core/tool_emoji.py`:

- `TOOL_EMOJIS` — a copy of the gateway's registry table, one emoji per tool name.
- `tool_emoji(name, is_error)` — override file → table → `⚙️` fallback; error
  calls return `❌` regardless of tool, preserving the old error semantics.
- `tool_label(name, is_error)` — the shared `"<emoji> [PROMPT_INJECTION]"` head line.
- `HERMES_TOOL_EMOJIS` (default `~/.hermes/reticulum_tool_emojis.json`) — a flat
  `{"tool": "emoji"}` override map, so an operator can re-sync after a Hermes
  update without waiting for a bridge release. Parsed once per process
  (`reset_cache()` to reload); unreadable file logs a warning and falls through.

All four render sites now call the module. Notes on the call sites:

- `ToolStep.summary()` imports the helper lazily inside the method — `core/__init__.py`
  imports `control_server`, so a module-level import would add a hop to the core
  import chain.
- `/tools` previously appended `" ❌"` itself *and* got one from `summary()`. That
  double-printed a failing tool. Now only `summary()` adds it.
- The head line is followed by a newline in `_push_step`, so the emoji goes on
  its own line; `❌ tool` followed by output reads unambiguously.

## Why copy the table instead of importing it

The bridge runs in its own venv and may be on a different host or Hermes
version than the gateway; `from agent.display import get_tool_emoji` is not
available in-process (and `hermes chat -q` runs a *child* process, so the
gateway's hook never fires for a mesh turn at all — see
`docs/mesh-bridge-findings-2026-08-29-step-watcher-recap-bug.md`). A copy is
unavoidable; the drift check and the override file are what make it safe.

## Keeping the copy honest

`tests/test_tool_emoji.py` includes a drift check against the installed gateway
which runs the real registry probe:

```
python -c "import model_tools; from tools.registry import registry;
           print({n: registry.get_emoji(n) for n in registry._tools})"
```

It asserts every tool the gateway knows maps to the same emoji here, and fails
naming the tool and both values. It skips (never fails) when Hermes is not
installed beside the bridge — the bridge must remain installable standalone.

### Two traps found while building that test

1. **The probe must use the gateway's interpreter, not the bridge venv's.** Run
   with the bridge venv, plugin discovery fails (`No module named 'httpx'`) and
   the registry comes back with **70 of 89 tools** — `terminal`, `read_file`,
   `write_file` all absent. The table comparison then passes over a partial
   surface while looking green. Fixed by preferring
   `~/.hermes/hermes-agent/venv/bin/python`, and by a separate
   `test_probe_sees_the_real_registry` that fails loudly when the sentinel tools
   are missing. A parity test over partial data is worse than no test.
2. **An inverted glyph predicate silently exempts everything.** The first
   version skipped any name whose emoji had no non-alphanumeric character. All
   real emoji *do* have one, so nothing was exempted; the intended exemption
   (upstream `xai_video_*` register `emoji="video"`, a bare word) only worked
   because those two tools were absent from the partial registry. Both are
   fixed; the exemption now checks for a real glyph and the red-path was
   confirmed by corrupting `terminal` → `🖥` and watching the test fail with
   `{'terminal': {'gateway': '💻', 'mesh': '🖥'}}`.

## Upstream note

`tools/xai_video_tools.py` registers `xai_video_edit` and `xai_video_extend`
with `emoji="video"` — a bare word, not a glyph. The gateway renders that
literally. A candidate one-line upstream fix (`emoji="🎬"`), worth mentioning if
a PR is opened.

## Coverage

`_SENTINELS` guards the probe; `test_table_covers_the_tools_the_bridge_gates`
checks `SAFE_TOOLS`/`RISKY_TOOLS` from `control_server` (the set the mesh
operator actually approves) all render a real emoji; `test_no_table_value_is_a_bare_word`
catches a future bad copy from upstream. 12 tests, full suite 76 green.