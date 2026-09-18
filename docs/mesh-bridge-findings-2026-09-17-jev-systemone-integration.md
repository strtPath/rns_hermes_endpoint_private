# Jev 1.13 (TypeSafe System One) — Integration Writeup

> Written: 2026-09-17 · Branch: `dev` · Predecessor: `pre-tool-callback-timeout-issue.md`
> Scope: survey + mesh-tool-gate integration design. Cordoned for later decision; nothing shipped yet.

## 1. What Jev actually is

**Jev 1.13 is a decision model, not a chat model.** It is TypeSafe's flagship *System One*
model — it takes natural-language/JSON *state* and returns **typed answers + calibrated
confidence** over three primitives:

| Primitive | Returns | Example |
|---|---|---|
| `Noul` | probability the answer to a yes/no question is yes | `{"type":"noul","noul":0.95}` |
| `Choice` | selected option + per-option probabilities + confidence | `{"type":"choice","choice":"billing","probabilities":{...},"confidence":0.596}` |
| `Score` | value on an ordered, descriptive scale + legend + confidence | `{"type":"score","score":1.035,"confidence":0.842}` |

**It does not generate text.** It cannot summarize, write code, or explain. Feeding it a
chat-style prompt yields a typed guess for a question with no closed answer space — garbage.

**Protocol / endpoint (critical, VERIFIED 2026-09-17 via live probe under an sk-or-v1 key):**
TypeSafe is a **native API and Python SDK**, *separate* from a standard chat-completions
surface. There are **two** working paths, both accepting the same `state`+`questions`
payload:

- **TypeSafe native:** `POST https://api.typesafe.ai/v1/systemone`,
  auth `Authorization: Bearer $TYPESAFE_API_KEY` (a TypeSafe key from console.typesafe.ai,
  **not** an OpenRouter key — OpenRouter keys return 401 here). SDK `typesafe-sdk` on PyPI:
  ```bash
  pip install typesafe-sdk          # or: uv add typesafe-sdk
  ```
  ```python
  from typesafe_sdk import TypeSafeClient, Choice, Noul, Score
  with TypeSafeClient() as c:
      r = c.system_one(
          state={"document": "I was charged twice. Fix ASAP."},
          questions={
              "billing": Noul(instructions="Is this ticket about billing?"),
              "tone": Choice(instructions="Customer tone?",
                             criteria={"calm": None, "frustrated": None, "angry": None}),
              "urgency": Score(instructions="How urgent?",
                               criteria=["can wait", "this week", "today"]),
          },
      )
      print(r.nouls["billing"].noul, r.choices["tone"].choice, r.scores["urgency"].score)
  ```
- **OpenRouter (USE THIS — your existing `sk-or-v1-...` key works):**
  `POST https://openrouter.ai/api/alpha/decisions` (VERIFIED live: return 200 with the
  full answers payload). A `/api/v1/...` or `/v1/systemone` path 404s. Confirmed live with
  the same `{"state":..., "model":"typesafe/jev-1.13", "questions":{...}}` payload, which
  returns `{"model":"typesafe/jev-1.13-20260917","answers":{...},"usage":{...},"provider":"TypeSafe"}`.
  The model slug on OpenRouter is `typesafe/jev-1.13`.

**Jev is NOT a chat-completions model.** OpenRouter itself confirms:
"typesafe/jev-1.13 is a decisions model and cannot be used with the chat/completions
endpoint." So it **must NOT** be wired into any Hermes slot that calls
`/v1/chat/completions` (`auxiliary.*`, `bedrock.auxiliary.*`,
`smart_model_routing.cheap_model`, delegation model, etc.) — it returns typed decisions or
fails. Jev lives behind its own client/endpoint, called *from your plugin code* as an HTTP
POST inside `mesh-tool-gate` (add `typesafe-sdk` or a plain `urllib`/`requests` call as a
dependency).

- **Function calling** (TypeSafe docs): maps natural-language requests onto typed functions
  and *closed-set* arguments by turning names/args into confidence-aware questions. This is
  an upstream confirmation that Jev suits *controlled tool dispatch*, not free-form tool use.

## 2. The three real levers in your Hermes setup

Current relevant state from `~/.hermes/config.yaml`:

1. **`smart_model_routing` — OFF, `cheap_model: {}` empty.** This is Hermes's *intended*
   lever for "cheap deterministic routing of simple requests." It routes short/simple
   user turns (≤160 chars / ≤28 words / ≤10% tools) to a configured cheap model instead of
   the main. **Jev can't drive it** (needs prose), but a cheaper chat model (e.g. the
   existing `deepseek/deepseek-v4-flash-0731` you already run as fallback, or stepfun)
   *can* — and it's off, so it's currently zero-cost no-ops.
2. **`mesh-tool-gate` plugin** (`~/.hermes/plugins/mesh-tool-gate/`) — fires on
   `pre_tool_call`, `MESH_GATE_TIMEOUT`=900s default, **fail-closed**. Crucially it is a
   **human-in-the-loop operator gate**: it pushes a prompt to a human control session and
   waits for `/approve` / `/deny`. It does *not* run an LLM verdict.
3. **Auxiliary model slots** (`auxiliary.approval`, `.triage_specifier`, `.curator`,
   `.monitor`, `.kanban_decomposer`, `.title_generation`, ...) — all `auto` currently. All
   expect prose/JSON-as-chat. **Not Jev-compatible** (protocol + output). Left alone.

## 3. Where Jev genuinely helps (the honest list)

Jev is *not* a money/feature shortcut for "the team model." It is a *decision accelerator*
for closed-set questions with real cost/latency savings where prose is currently wasted.
Concrete candidates, best-first:

1. **Pre-tool triage gate (the flagship)** — see §4. Automate the
   *"is this mesh tool call {allow / escalate-to-main / deny}"* decision cheaply and
   deterministically, *before* touching a reasoning model or a human.
2. **LLM-input/output guardrail** (TypeSafe "Guardrails for LLMs" cookbook) — one request,
   a battery of Nouls + a Score, thresholded in code → pass / review / block / route.
   Directly reusable as a *pre-tool* and *post-tool* hazard screen on the mesh bridge.
3. **Skill suggestion** (TypeSafe cookbook written *for Hermes*) — the 182-skill roster:
   one Jev request ranks all skills + asks "does this turn need one at all," a second
   verifies the top-3. Their published numbers: wrong-skill loads 16.8% → 7.3%, and
   "loads one when nothing fits" 9.8% → 4.0% (over 488 requests). **This is the single
   highest-value, lowest-risk Hermes-native win available.**
4. **Intent routing / ticket triage** in the control-plane — e.g. classify inbound
   bridge/mesh messages into routing buckets before a human/LM handles them
   ("Intent routing" + "Confidence-gated routing" patterns).

Counter-signal: **do not** push any of the Aguirre/`deepseek-v4-flash-0731`-replacing
ambitions toward Jev. It does not reason, write, summarize, or search. Its skill is
*narrow judgment*, its cost is near-zero ($0.042/M in, $0 out), so it is only useful at
high call volume, pre-decision points.

## 4. The mesh-tool-gate integration design (flagship, deferred)

> Status: **design only.** No key owned → no live probe; scheme below is swappable once a
> `TYPESAFE_API_KEY` is available. Do not land against the operator gate blindly.

**Why it complements (not replaces) the existing gate:** today `on_pre_tool_call` pushes
every mesh command to a *human* and fail-closes on the 900s timeout. That is correct for
authorization but expensive for the *common case* (benign, routine calls). The
`/approve|/deny` verdict flow can't tell "harmless" from "needs a human" without any model
in the loop. Jev slots in **in front of** the human handoff as a cheap triage stage.

**Proposed flow (`on_pre_tool_call`, Jev-before-human):**

1. Build `state` from the tool call (name + `_build_description` blob, already in the
   plugin) + a compact risk context (originating mesh session, command class).
2. One Jev `system_one` call, questions:
   ```python
   {
     "risk":        Noul(instructions="Would executing this tool call harm the mesh, the host, or another node if run as requested?"),
     "is_authorized": Noul(instructions="Is this the kind of call the operator routinely approves without review?"),
     "stakes":      Score(instructions="How consequential is acting on this call?", criteria=["routine", "sensitive", "destructive"]),
     "needs_human": Choice(instructions="How to handle this call?", criteria={"allow": None, "escalate": None, "deny": None}),
   }
   ```
3. Route in code (deterministic checks, per TypeSafe "keep code in control"):
   - **allow** when `needs_human == "allow"` AND `risk.noul < 0.2` AND `stakes.score <= 0.5` —
     proceed **without** touching the human gate or a reasoning model. (Cheap, fast, no 900s wait.)
   - **escalate** when `needs_human == "escalate"` OR confidence < threshold → fall through
     to the **existing** operator gate (human `/approve|/deny`), preserving fail-closed.
   - **deny** when `needs_human == "deny"` OR `risk.noul > 0.8` → block immediately with
     the existing `_block_message` wording.
4. **Confidence gate everything** (`confidence-gated routing`): a universal **floor**
   (e.g. <0.6) on any answer → escalate. High-stakes actions demand *higher* confidence
   (e.g. `stakes >= destructive` requires `confidence >= 0.9`) before auto-allow. Test
   thresholds by plotting confidence vs. accuracy on your own gate history before trusting.

**Safety constraints to hold:**
- Jev verdicts are **advisory**, never the sole authority for destructive/privileged
  actions — permanently keep the human operator gate as the backstop.
- **Fail-closed preserved:** any Jev error, timeout, or low confidence → escalate, never
  allow. Mirror `_gate_tool`'s fail-closed default (`(False, verdict, '')`).
- **No sensitive data in `state`:** the bridge already quarantines PII (see
  `mesh-bridge-findings-2026-09-16-pii-quarantine-old-dev-lineage.md`) — keep `state`
  minimal and non-PII.
- One extra outbound call per gate is a new latency/failure surface; keep Jev distinct from
  the reasoning path so an outage degrades to "escalate" (human gate still works), not
  "deny everything."

## 5. Cost / savings framing (does this actually save money?)

- Jev: **~$0.042 / 1M input tokens, $0 / 1M output** (OpenRouter), 32K context.
- **Measured live (2026-09-17, one gate probe over OpenRouter decisions endpoint):**
  398 input + 69 output tokens → **$0.0000167 per call, ~1.4s latency**.
  Even 10,000 gated calls ≈ $0.17/year of decision cost. Cost ceiling is a non-issue.
- The thing Jev replaces in the fast path is *not* a cheaper LLM call (there isn't one in
  the current gate — it goes straight to a human) — so the **saving is human/operator time
  and 900s gate stalls on routine calls**, plus *deterministic latency* (tens of ms vs.
  seconds). It also *avoids* paying a reasoning model to do safe-or-not classification.
- **Actual cash saving** only materializes if Jev *reduces the volume of expensive LLM
  calls* — e.g. the skill-suggestion path (probe: currently `auxiliary.skills_hub` is
  `auto`, but skill *selection* in-session is prose-driven; Jev two-call selection cuts
  wrong loads by half, which cuts wasted context/tokens across the session). That is the
  more defensible dollar-saving integration.

## 6. Integration status (option 1: skill-suggestion — LANDED 2026-09-17)

**Decision: option 1 (skill-suggestion) chosen and built as a Hermes plugin.**

Key correction to the TypeSafe cookbook as written: its mechanism (inject
`<skill_relevance>` into the *system prompt*) would violate Hermes' core
invariant — "the system prompt is byte-stable for the life of a conversation"
(root AGENTS.md; prompt caching is sacred). The cookbook assumes that slot;
real Hermes does not have it. The *intent* is still legitimately reachable via
Hermes' sanctioned per-turn channel:

- Hook `pre_llm_call` may return `{"context": "..."}` (or str), which Hermes
  injects into the **user message** (never the system prompt) —
  `agent/turn_context.py::_collect_pre_llm_call_context`, contract in
  `hermes_cli/plugins_dispatch.py`. The gateway already uses this same channel
  for per-turn notes. Cache-safe.

So the port is a **plugin** at ~/.hermes/plugins/ (footprint-ladder rung 4,
"capability at the edges" — no core patch needed):

- `jev-skill-suggest/` — plugin.yaml (`hooks: [pre_llm_call]`) + `__init__.py`
  + `jev_roster.json` (prebuilt 232-skill roster: name + frontmatter description).
- On each `pre_llm_call`: one Jev Choice over the full 232-skill roster (+ a
  `__none__` bucket so Jev can say "nothing fits"); if the top pick's
  probability >= 0.5 and it is not `__none__`, inject a one-line
  `<skill_relevance>` hint into the user message; otherwise return nothing.
  Fail-open on any Jev error / low confidence / missing key.
- Key: `OPENROUTER_API_KEY` from `~/.hermes/.env` (loaded into the runtime env
  for plugins, same as the bedrock/aux config keys).

Live verification (all real Jev calls via OpenRouter `/api/alpha/decisions`):
- deploy an LLM inference server with vLLM  -> serving-llms-vllm ✓ (1.2s)
- review rns_hermes_endpoint plugin for security holes -> repo-security-audit ✓ (1.7s)
- "what's the weather like today"            -> __none__, no injection ✓ (0.8s)

Cost: ~7K input tokens per call @$0.042/M ≈ **$0.0003/call** (~$3/10k turns).
Latency ~1.5s only when the call fires; gated by confidence, so most turns cost
nothing and the turn is never blocked on timeout.

Note (cordoned, not implemented): actual *load* of the suggested skill still
goes through the normal agent flow (the hint tells the agent which skill to
look at first; it does not force a load). A future step could auto-invoke
`skill_view` on the winner, but that's a behavior change to the agent loop and
is left out of v1.

## Remaining options (cordoned — revisit)

Live-probe status as of 2026-09-17: **endpoint + key CONFIRMED WORKING.** OpenRouter
`POST /api/alpha/decisions` with an `sk-or-v1-` key answers the TypeSafe payload correctly
(see §1). Remaining work, in priority order:

1. ~~**Ship skill-suggestion first**~~ — DONE, see above (plugin `jev-skill-suggest`).
2. **Then** build the mesh-tool-gate triage layer (§4) as a new plugin or a fast-path inside
   the existing gate, keeping the human gate as backstop. Use
   `openrouter.ai/api/alpha/decisions` (verified 200; `/api/v1/decisions` 404s) with the
   existing `sk-or-v1-` key (no TypeSafe signup needed). `docs/probe_jev.py` is a working
   reference client.
3. Self-document each landed piece in `docs/` following the existing findings convention,
   including a small fixture set + confidence-vs-accuracy calibration check before raising
   any auto-allow threshold.

## 7. Sources

- TypeSafe System One: https://docs.typesafe.ai/concepts/system-one
- Quickstart (curl + request/response bodies): https://docs.typesafe.ai/introduction/quickstart
- Python SDK: https://docs.typesafe.ai/sdk/python
- Confidence-gated routing: https://docs.typesafe.ai/patterns/confidence-routing
- How to build with System One: https://docs.typesafe.ai/concepts/how-to-build-with-system-one
- Cookbooks: skill-suggestion (Hermes), llm-guardrails, intent-routing, function-calling, sde-cascade
- OpenRouter model page: https://openrouter.ai/typesafe/jev-1.13
- Local: `~/.hermes/plugins/mesh-tool-gate/__init__.py`, `~/.hermes/config.yaml`