---
title: Hermes CLI Interaction Contract
level: sub-spec
parent: spec-rns-hermes-endpoint.md
subsystem: hermes-cli-contract
status: DRAFT
---

# Hermes CLI Interaction Contract

## Scope

This document owns the contract between the bridge and the Hermes CLI: how a turn is
spawned, what interface is driven, what that interface guarantees, and what it refuses
to do. It does not cover what happens to a message after the child returns (turn-lifecycle
sub-spec), the approval gate or step watcher (approval and step-push sub-spec), or
Reticulum transport.

Status note: written from direct reading of the two codebases on 2026-09-23, after a live
mesh test produced behaviour that contradicted the bridge's assumptions. Every claim below
was read from source, not inferred.

## 1. The spawn command

The bridge runs one child process per turn. The command is built in
`src/hermes_reticulum/core/hermes_client.py` around line 1326:

    hermes chat -q <message> --source <source_tag> -Q
        and one of: --resume <session_id>
                    -c <session_name>            (if the session already resolves)
                    -c <session_name> --create-if-missing   (if the build supports it)
                    nothing                      (fresh session, adopted afterwards)
        and optionally: -m <model>               (only if a model is configured)

`extra_args` from config are appended last.

What each flag does, and why it matters:

`-q <message>` is the query flag. It hands the prompt to the CLI. This is also the flag
that selects the headless single-query mode, and that is the source of the clarify
problem documented in section 4.

`--source <tag>` labels the session's origin so the bridge can find its own sessions.

`-Q` is quiet mode: "suppress banner, spinner, and tool previews. Only output the final
response and session info." Its help text states that single-query behaviour is
"implied on non-TTY stdio and by -Q/--quiet."

`--resume` continues a known session. `-c` names a session. The fresh-session case (no
`--resume`, no resolvable name, no `--create-if-missing` support) starts an untitled
session and adopts it afterwards; see `_adopt_new_session`.

`-m <model>` is only added when a model is configured. Note that this bridge also reads
`HERMES_MODEL` from the environment at line 184: `self.model = model or
os.getenv("HERMES_MODEL", "").strip() or None`. When that env var is set it silently
outranks whatever `~/.hermes/config.yaml` says the default model is.

## 2. What the CLI actually guarantees

Read from a local Hermes agent checkout.

The `chat` subparser (`hermes_cli/_parser.py` around line 213) documents the intent of
`-q` directly:

    "Query to run. On a real TTY the prompt seeds an interactive session (submitted
     literally as the first turn); combined with --oneshot or -Q, or on a non-TTY, it
     answers and exits."

That single sentence is the contract. Three conditions force the headless one-shot path:
a non-TTY stdin, `-Q`, or `--oneshot`. The bridge's child has no TTY and passes `-Q`, so
it is doubly locked into the headless path.

`hermes_cli/cli_single_query.py::_run_single_query_mode` (line 426) implements this. At
line 438 it sets:

    cli._single_query_mode = True

with the comment: "agent waits the full MCP cold-start before its only tool snapshot."
Once that flag is set, the run is one-shot by construction.

So the CLI's guarantee is: with `-q` plus `-Q` (or without a TTY), you get answer-and-exit,
one turn, no interactivity. What it does NOT guarantee is that a human is reachable during
the turn. It explicitly assumes the opposite.

## 3. The liveness guard

(To be completed. Measures stream-idle time, not process time. Timeout from
HERMES_LIVENESS_TIMEOUT. A silent turn can be self-killed; see the code -9 findings
document for the original incident.)

## 4. Consequence for clarify

This is the finding that matters most, because it invalidated a day of debugging.

In the headless path, Hermes installs its own clarify callback rather than using the
interactive one. `hermes_cli/cli_agent_setup_mixin.py` line 14 defines
`_single_query_clarify_callback`, and line 654 selects it:

    single_query_mode = getattr(self, "_single_query_mode", False)
    clarify_callback = (
        _single_query_clarify_callback
        if single_query_mode
        else self._clarify_callback)

The callback never waits. Its docstring states why:

    "A -q turn never builds the prompt_toolkit app, so the interactive clarify modal can
     never be painted or answered. The CLI callback would poll until
     agent.clarify_timeout while the caller sees a silent hang. Mirror the oneshot path
     and answer immediately instead."

It returns, for a question with choices:

    [single-query mode: no user available to answer '<question>'. Pick the best
     option from ['<first> (Recommended)', ...] using your own judgment and continue.]

The referenced issue is #94943.

Consequences, stated plainly:

A clarify call in a mesh turn does not block. It returns immediately, telling the model
to choose for itself and carry on. The model then picks the first (recommended) choice and
continues the turn. The turn ends. Any answer the user sends afterwards arrives at a turn
that has already closed.

This is why no timeout value would have fixed the symptom. There is no wait to time out.
Raising `approvals.timeout` or `agent.clarify_timeout` changes nothing, because the code
path taken never reaches the waiting logic. `approvals.timeout` is unrelated to clarify in
this mode.

This is deliberate upstream behaviour, not a bug in Hermes. It is correct for every normal
one-shot caller: cron jobs, scripts, pipes. The bridge is the unusual case, a one-shot
caller that nonetheless has a live user reachable over the mesh.

## 5. The seam that exists and that the bridge does not use

`agent.clarify_callback` is a settable attribute. The gateway sets it after constructing
the agent, at `gateway/run_turn_runner.py:1289`:

    agent.clarify_callback = self._clarify_callback_sync

`tui_gateway/agent_callbacks.py:142` supplies its own as well. So the mechanism for "headless
process, live user" exists and is used by other front-ends.

The blocking machinery the gateway builds on is `tools/clarify_gateway.py`: a
session-scoped registry of pending questions, each with an event, plus `register_notify`
(a per-session callback), `resolve_gateway_clarify`, and `resolve_clarify_timeout`, which
reads `agent.clarify_timeout` from config and defaults to 3600 seconds. Its own docstring
references issue #32762, an earlier case of an entry being evicted while the agent waited.

The bridge cannot reach this seam. It drives the CLI as a subprocess, so it has no access
to the agent object inside the child. And no plugin hook can reach it either:
`VALID_HOOKS` in `hermes_cli/plugins.py` covers tool calls, LLM calls, streams, API
requests and auxiliary calls. None of them fire at agent construction, which is the only
moment the callback is set.

## 6. What the bridge assumes vs what the CLI guarantees

Assumption: the child will wait for a user when one is needed.
Reality: in this mode the CLI assumes no user exists and self-answers. Not guaranteed.

Assumption: an answer sent after a clarify question will reach the turn that asked it.
Reality: the turn has already completed and closed by the time the answer arrives.

Assumption: a clarify timeout is a timeout, and tuning it will change the behaviour.
Reality: the waiting path is never entered. No timeout is involved.

Assumption: `-Q` only affects output formatting.
Reality: `-Q` is one of the conditions that forces the headless one-shot path, which is
where the clarify behaviour comes from.

Assumption: a model set in `.env` is the same as letting Hermes pick its default.
Reality: `HERMES_MODEL` outranks the config default silently.

## 7. Open questions and unknowns

How the child's stdout is parsed, and how fragile that parsing is to output changes.
Whether the fresh-session adoption path can race with the watcher on a first turn.
Whether any supported flag combination gives a single query AND a live clarify callback,
without embedding the agent.

## 8. Invariants observed

The child never has a TTY.
The child is always in single-query mode, so any tool that expects interactive input
(not just clarify) takes its headless path.
Session identity for the first turn of a new session is not known when the turn starts.
