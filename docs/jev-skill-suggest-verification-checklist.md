# Jev Skill-Suggest — Post-Restart Verification Checklist

> **Writeup deep-dive:** `docs/mesh-bridge-findings-2026-09-17-jev-systemone-integration.md`
> **Script under test:** `docs/probe_jev.py`

## Status: SHIPPED, ENABLED, and CONFIRMED LOADED (2026-09-18)

The plugin `~/.hermes/plugins/jev-skill-suggest/` is live. On the restart after it was
added to `plugins.enabled`, `agent.log` line ~94 shows it capability-checked and loaded.
A fresh process driven through Hermes's real `PluginManager.discover_and_load()` confirms:

```
plugin loaded: True
plugin enabled: True
plugin error: None
pre_llm_call callbacks registered: 1
  - on_pre_llm_call
```

## RUNTIME KEY: CONFIRMED ✅
`OPENROUTER_API_KEY` from `~/.hermes/.env` (line 406, uncommented) IS available to the
plugin at runtime. Tested by exporting the live key and invoking the real
`on_pre_llm_call` against the real Jev endpoint:

- "review the rns_hermes_endpoint plugin for security holes" → hint for `repo-security-audit` ✅
- "just chatting about the weather" → no hint (gated) ✅

No "Jev call failed (No API key...)" appeared in the log.

## TWO-MODE ENHANCEMENT (added 2026-09-18, requires restart to go live)

Verified standalone by driving the real `on_pre_llm_call`:

| Model | Mode | Result |
|---|---|---|
| any (e.g. deepseek-v4-flash) | HINT | one-line `<skill_relevance>` hint when confident |
| weak local quant (qwen3.8-27b-iq3_xxs) | LOAD | full scaffolded skill body via `build_preloaded_skills_prompt` when confident |
| any / chitchat | (none) | no injection (fail-open + confidence gate) |

Weak-model detection = `_WEAK_MODEL_PATTERNS` substrings in the active model name
(`iq`, `q4_`, `gguf`, `qwen3.8`, ...). Weak models get the skill BODY spelled out because
they follow tools/skills poorly; strong models stay hint-only to save tokens.

## LIVE-FIRE OBSERVABILITY (added this pass)

`on_pre_llm_call` now `logger.info`s every HINT/LOAD injection and `logger.debug`s every
fire, so a real turn is observable in `~/.hermes/logs/agent.log`:
- `jev-skill-suggest: on_pre_llm_call fired (model='...')` (debug)
- `jev-skill-suggest: HINT mode - skill X (p=0.9, weak=False)` (info)
- `jev-skill-suggest: LOAD mode - weak model '...', skill X (p=...)` (info)

## REMAINING OPEN ITEM (needs a Hermes restart to act on)

The running gateway loaded the PRE-enhancement code at startup. The logging + load-mode
are in the file but not active in the live process. After the next restart:

1. `grep 'jev-skill-suggest' ~/.hermes/logs/agent.log` — expect at least the `HINT mode`
   info line on a genuine skill-bearing turn (e.g. the security-review probe).
2. Verify LOAD mode by switching the main model to the local `qwen3.8-27b-iq3_xxs` and
   sending a skill-bearing turn — expect `LOAD mode - weak model` + the full skill body
   riding in that turn's user message.
3. Confirm a chitchat turn logs no injection (debug-gated, no info line).

## Follow-ups

- [x] Auto-`skill_view` on the winner (implemented as load-mode for weak models).
- [ ] Consider a config.yaml knob for `_WEAK_MODEL_PATTERNS` (currently hardcoded).
- [ ] Tune `_CONF_THRESHOLD` (0.5) once real-turn data exists.
- [ ] mesh-tool-gate Jev triage layer (§4 of the 09-17 writeup) — separate decision.