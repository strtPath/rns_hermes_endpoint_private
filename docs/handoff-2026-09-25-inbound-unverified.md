---
title: Handoff — inbound still unproven; read the spec before more code
date: 2026-09-25
status: open
---

# Where this stands, and what to do next

## The ask

Stop and re-assess against the spec. This file is the state so a fresh session
does not re-derive it — several claims in this session were wrong and got
corrected only by live checks, so carry the corrections, not the reasoning.

## What is DONE and verified

1. **Path-aware delivery + propagation policy** (`dab188c`). Probe the path
   before choosing a method, watch the message to a terminal state instead of
   reporting acceptance, `RETICULUM_PROPAGATION_NODE` (auto | off | pinned
   hash). Live-verified: path request resolves the peer in 0.5s where the old
   `DIRECT` default cancelled after one try.

2. **The echo defect** (`798bbd6`). One field, `_delivery_cb`, served both
   directions: `register_delivery_callback` set it to the INBOUND handler, and
   `send_to` attached our own outgoing message's delivery callback to that same
   field — so every reply was parsed by the inbound handler and re-entered as
   peer traffic. Proven live before the fix (`INBOUND handler fired 1 time(s)
   from an OUTBOUND send`, payload being the text just sent).

   Now two declared slots: `register_inbound_callback(cb) -> cb(source_hash,
   payload)` and `register_outbound_callback(cb) -> cb(payload, state)`. Live
   after: inbound fired 0, outbound fired 1 with `state=DELIVERED`.

   Also fixed in the same commit: a SECOND invented API,
   `LXMF.LXMessage.state_name()`, which does not exist. Both directions of the
   state mapping now live in `plugin/delivery.py`.

   Findings: `docs/mesh-bridge-findings-2026-09-25-inbound-outbound-callback-echo.md`

3. **Identity unification.** The adapter generated its own identity
   (`gateway_identity`) while the bridge held a separate one
   (`~/.lxmf/storage/hermes_identity`, stable since July). Copied the bridge's
   identity over the adapter's, moved the old LXMF store aside, backed up the
   old identity. The adapter now derives the bridge's hash. Verified by loading
   the file and computing the destination.

4. **Config.** `RETICULUM_PROPAGATION_NODE` pinned in `~/.hermes/.env`. Posted
   by me into the repo: `docs/mesh-bridge-findings-2026-09-25-path-aware-delivery-and-propagation.md`.

## What is NOT verified

**A message from the peer has never once arrived.** That is the whole open
question. Everything above is outbound, or inbound only in simulation
(`_on_router_delivery` called with a synthetic message).

Not verified:
- That the adapter's announce reaches the peer's client.
- That the peer's client addresses the adapter's hash (it may hold the
  adapter's short-lived identity cached from the last day).
- That a real inbound message fires the router callback at all.

## The thing to do FIRST, before more code

**Read `docs/spec-rns-hermes-endpoint.md`, section 5.** The skill says this is
where the "implemented correctly, still wrong" surprises come from, and this
session produced exactly that shape twice. Then read
`docs/spec-reticulum-platform-adapter.md`, because it supersedes the feasibility
doc on all design questions and this session edited adapter code without
re-reading it.

The specific question worth settling there: **what is the intended relationship
between the bridge and the adapter?** Both are currently present in the tree,
both can run, and the adapter's storage defaults to a path that gives it a
different identity from the bridge — which is how two identities came to exist
in the first place. The spec may already answer whether they are meant to
coexist during a migration or whether one is meant to replace the other.

## Corrections to carry forward

Things asserted in this session that were WRONG, so they do not get repeated:

- **"Nothing is being heard."** From reading `path_table: 0` off a probe that
  built its own RNS instance. The daemon had received 304,627 announces. This
  is the second time this session the same probe mistake produced a false
  claim — match the attachment mode of the process you are reasoning about.
- **`send_to` returning `True` means accepted, not delivered.** Already in the
  skill; it happened again anyway.
- **`LXMF.LXMessage.state_name()` does not exist.** LXMessage declares state
  constants only (`LXMessage.py:14-23`).
- **The test suite asserted the echo defect as correct.** A test can encode a
  defect as its expectation, so green proves nothing about the system.

## Environment facts that cost time

- The gateway cannot be restarted from inside it — Hermes refuses the command
  because SIGTERM propagates to the child. Hand it to the user.
- A plain `systemctl --user` fails on this host with a DBus error;
  `hermes gateway restart` works because `_ensure_user_systemd_env()`
  (`hermes_cli/gateway.py:2284`) sets `XDG_RUNTIME_DIR` and
  `DBUS_SESSION_BUS_ADDRESS` before every `systemctl --user` call.
- Two RNS installs: the gateway venv is **1.5.2**, the repo venv is **1.5.4**.
  The adapter logged `RNS 1.5.2` at its last start. The skill says to pin both
  to the same version; check whether that is what actually happened.

## Uncommitted on purpose

Nothing. Tree is clean at `798bbd6`. The moved-aside artifacts are
`.hermes/.reticulum-gateway/storage/lxmf.old-identity.*` and
`gateway_identity.bak.*` — kept, not deleted, because they are the only record
of what was live during this window.
