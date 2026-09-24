---
title: Reticulum Platform Adapter — Design Spec
level: sub-spec
parent: spec-rns-hermes-endpoint.md
subsystem: platform-adapter
supersedes: spec-feasibility-reticulum-platform.md (feasibility stage, now settled)
status: DRAFT
---

# Reticulum as a Gateway Platform Adapter: Design

## Scope

States how the mesh bridge becomes a Hermes gateway platform adapter, what the adapter owns,
what the gateway owns, and what gets deleted. This is the design document; the feasibility
document answered whether it was possible and is superseded by this one on all design
questions.

Parent: `spec-rns-hermes-endpoint.md`. Evidence: `_notes-reticulum-fit.md`,
`_notes-delivery-confirmation.md`.

Status: DRAFT, 2026-09-24. Written from source (rns 1.5.4, lxmf 1.1.1) and the gateway's own
platform contract (`gateway/platforms/ADDING_A_PLATFORM.md`, `base.py`). No adapter exists yet.

## 1. The decision, and why this shape

The bridge currently spawns `hermes chat -q` children and reimplements session handling,
tool-call streaming, clarify, and approvals outside the gateway. Every one of those is
something the gateway already implements properly for its other platforms.

The adapter form is chosen because it deletes that machinery rather than fixing it. The
decision rests on evidence gathered 2026-09-24 and it holds: see section 3.

This is a plugin, not a core contribution. `ADDING_A_PLATFORM.md` states the plugin path
"requires zero changes to core Hermes code", and out-of-tree registration is the documented
recommendation for community platforms. Nothing here needs a PR into Hermes.

## 2. What the adapter is

A plugin directory with `plugin.yaml` and `adapter.py`, inheriting `BasePlatformAdapter`, and
registering through `ctx.register_platform()` in a `register(ctx)` entry point. The plugin
system then provides, without adapter code: adapter creation, config parsing, user
authorization, cron delivery, `send_message` routing, system prompt hints, and status display.

**Required surface.** Four abstract methods and the typing hook:

- `connect(is_reconnect=False) -> bool`
- `disconnect()`
- `send(chat_id, content, reply_to=None, metadata=None) -> SendResult`
- `get_chat_info(chat_id) -> dict` with at least `name` and `type`
- `send_typing(chat_id)` — inherited default is acceptable

**Optional media methods** (`send_image`, `send_document`, `send_voice`, `send_video`) have
default stubs that return failure. LXMF supports attachments as raw payloads, so these could
be implemented later. Out of scope for the first version (section 9).

**Capability flags to set:**

| Flag | Value | Reason |
|---|---|---|
| `supports_async_delivery` | `True` | The mesh can start a fresh turn after a previous one ended. Correct, and the constraint is only that the message actually arrives. |
| `splits_long_messages` | `True` | LXMF over radio benefits from chunking. The gateway otherwise truncates at `MAX_MESSAGE_LENGTH` (4096). |
| `supports_status_text` | leave `False` | No typing-indicator concept on LXMF; the gateway handles absence. |
| `supports_code_blocks` | `True` | Reticulum clients now render markdown, including fenced code blocks. Verified live on Sideband: a fenced block arrives as a rendered block, not raw fences. |
| `REQUIRES_EDIT_FINALIZE` | leave `False` | No edit support; `edit_message` returns `success=False` and callers send anew. |

**Hooks to implement** (all optional, all needed here):

- `env_enablement_fn` — seed `PlatformConfig.extra` and the home channel from env before
  construction, so env-only setups appear in `hermes gateway status`. Build from a
  `(ENV_VAR, extra_key, conv)` table via `_shared.seed_extra_from_env`.
- `apply_yaml_config_fn` — own the YAML schema for this platform rather than growing core
  config boilerplate. Return `_shared.apply_yaml_bridge(platform_cfg, TABLE)`.
- `is_connected` — use `_shared.env_is_connected(...)`.
- `cron_deliver_env_var` — names the home-channel env var so `deliver=reticulum` cron jobs
  route without editing the scheduler's hardcoded sets.
- `standalone_sender_fn` — required for cron jobs that run out-of-process; without it a
  `deliver=reticulum` job fires but the send returns `No live adapter`.

**Every env read goes through `_shared.get_scoped_secret`.** Never `os.getenv` directly: under
`gateway.multiplex_profiles` a raw read returns the default profile's value, which is a
silent allowlist leak. Import the shared reader, never re-implement it.

## 3. The delivery finding, settled

The feasibility document left one gate open: does this deployment deliver direct or through a
propagation node. It is answered, and the answer favours the adapter.

Measured from the running bridge's journal over 14 days:

- 2,269 `state=delivered`, 10 `state=timeout`, 0 propagated.
- That is 99.4% direct delivery with first-hop cryptographic acknowledgement.

So the delivery mapping is a translation, not a design problem. `PacketReceipt.DELIVERED` and
`LXMessage.FAILED` map onto `SendResult.success` honestly.

The caveat matters for a public plugin and must not be lost: this deployment tracks direct
because its peers are reachable. A plugin shipped for general mesh use will meet propagation
nodes, offline peers, and store-and-forward measured in days. The adapter must therefore
handle both, and section 6 states what it does in each case.

## 4. Identity mapping

| Gateway concept | Reticulum value |
|---|---|
| `chat_id` | LXMF destination hash |
| `user_id` | same hash for a 1:1 DM (Signal does the same) |
| `chat_type` | `dm` |
| `chat_name` | adapter-owned hash to display-name map |
| `thread_id` | none; single-threaded per peer |

Names are adapter-side because the gateway keeps no hash-to-name mapping and RNS has no
contact directory. The adapter maintains one, seeded from config, and `get_chat_info` is not
on a hot path, so staleness is tolerable.

**Key rotation breaks session continuity.** If a peer rotates its identity the destination
hash changes, the session key changes, and the gateway has no chat-id migration mechanism. The
adapter can hold an old-to-new hash map, but the gateway will not follow it. Rare, and it
silently loses a conversation when it happens. Named as an accepted limitation, section 8.

## 5. Transport bridging

RNS calls back from its own event-loop thread, not asyncio. The standard pattern applies:

1. `connect()` starts the RNS thread (and the LXMF router).
2. RNS callbacks push onto a queue.
3. An asyncio task drains the queue and calls `self.handle_message(event)`.

The drain task must be cancelled on `disconnect()`, and the RNS thread joined. No existing
in-tree adapter runs a persistent transport thread, so this is new ground for the codebase
rather than a copied shape. `api_server.py` uses `threading.Thread` for a one-shot task, which
is a precedent but not the same thing.

**`connect()` semantics are strained and this is accepted.** The gateway reads `True` as "the
platform is live and will push inbound". For Reticulum, `True` can only mean "the local RNS
instance is running"; whether any given peer is reachable is separate and changes
continuously. The reconnect watcher keys off connect failure, so a silent peer never triggers
it. The adapter returns `True` on RNS up and reports peer reachability through a separate
surface (section 7), because the gateway has no vocabulary for connected-but-unreachable.

`connect()` must be idempotent and able to re-initialise RNS from scratch, since the watcher
calls it with `is_reconnect=True` after an RNS process death.

## 6. Delivery handling — the part that does not map

`SendResult` is binary. The gateway treats acceptance as delivery, records an obligation in
its SQLite ledger before `send()` (`record_obligation`), and finalises on return
(`mark_delivered` / `mark_failed`). There is no queued or pending state.

**Direct delivery.** `send()` registers a delivery callback on the LXMF message, awaits the
bounded outcome, and returns `success=True` on `DELIVERED` or `success=False` with an error
string on `FAILED`. The bound is five attempts at `DELIVERY_RETRY_WAIT` (10s) plus receipt
timeouts, which is seconds to low minutes. This is honest and complete.

**Propagated delivery.** Node acceptance fires a callback with `state = SENT`, not
`DELIVERED`. No per-message signal reaches the sender afterwards, ever. The adapter must
therefore:

- Return `success=True` on node acceptance, because the gateway offers no alternative, and
- Record the message in its own pending set, and
- Surface unconfirmed messages through the adapter's own status, since the gateway's ledger
  will have marked them delivered and will never correct itself.

This is the one place where bridge machinery survives the pivot, and it survives because it
solves a real problem peculiar to radio. It is not a workaround for a missing gateway feature.

**No silent queue at the packet layer.** `Packet.send()` returns `False` immediately when no
interface can carry the packet. The adapter maps that to
`success=False, retryable=True, error_kind=transient`, which lets the base class's retry logic
and the ledger's sweep behave correctly.

**`_send_with_retry` caveat.** The base class does not retry timeouts ("may have delivered").
A long direct send that times out therefore returns failure without retry, which is the right
call and should not be overridden.

## 7. What replaces each bridge subsystem

| Bridge component | Disposition |
|---|---|
| Session adoption heuristics | Deleted. The gateway owns sessions. |
| Step watcher polling `state.db` | Deleted. The gateway emits tool events directly. |
| Clarify gate and answer capture | Deleted. The gateway sets `agent.clarify_callback` in-process where a real user exists. |
| Approval control server | Deleted as a component. Its **behaviour** moves to the gateway's own approval path, which is strictly richer: a shared prompt template (`_format_exec_approval`), a plain-text fallback for adapters without buttons (correct for LXMF), and natural-language acceptance ("yes", "ok", "confirm", "y", 👍) routed to the canonical `/approve` handlers, not just the slash forms. **The 900s window is the part that must not be lost**: it exists for LoRa hop latency, dropped connections, and AFK users before deny-by-default, and the gateway's default `approvals.timeout` is 300s. Ship 900 as this platform's documented default and say why. |
| ACL | **Replaced, not removed.** The plugin auth hooks cover the same ground: `allowed_users_env` / `allow_all_env` on `ctx.register_platform`, read through `_shared.platform_gate_env` (multiplex-safe). The mesh's existing model carries over one-for-one: an allowlist by destination hash, allow-all off by default, and 32-hex validation. Document that the allowlist holds **LXMF destination hashes**, since that is what users will paste in. |
| Downlink queue and ack tracker | Survives, as the adapter's propagation-delivery tracker (section 6). |
| Slash commands | Deleted. The gateway's command set applies; mesh-only commands do not carry over. |
| Liveness guard / SIGTERM handling | Deleted. No child process to guard. |
| Preflight checks | Deleted for the bridge; RNS start-up failure surfaces through `connect()`. |
| `/status`, `/tools`, `/verbose`, `/steps`, `/hold`, `/go` | Deleted. Not in scope for the adapter; revisit only if a mesh user asks. |
| `/approve`, `/deny` | **Retained**, but served by the gateway's handlers rather than the bridge's. |

Commands that the gateway already provides and mesh users keep: `/new`, `/stop`, `/approve`,
`/deny`, `/status`, `/help`, `/model`.

Approach taken to the two rows above: nothing is deleted that the gateway cannot do at least
as well. Where the bridge holds a hard-won value (the 900s approval window), that value moves
into gateway configuration rather than being discarded with the component that held it.

Peers reachability, which the gateway cannot express, is reported in `send()` failures and in
the adapter's own `get_chat_info` metadata rather than invented as a gateway state.

## 8. Accepted limitations

- **Peer reachability is invisible to the gateway.** It will believe the platform is live
  while no peer can be reached. Reported adapter-side, not gateway-side.
- **Key rotation breaks a conversation.** No migration mechanism exists.
- **Propagated delivery is unconfirmable.** The adapter tracks it; the gateway's ledger will
  disagree, and the adapter must not fight that.
- **No media in v1.** Attachments are out of scope.
- **`chat_type` is always `dm`.** LXMF has no group concept in standard use. A group would
  need a distinct adapter shape and is out of scope.

## 9. Out of scope

Do not build these in the first version, and do not gold-plate toward them:

- Media send/receive over LXMF.
- Group or channel support.
- Propagation-node administration commands.
- Migration of existing bridge sessions into gateway sessions. The adapter starts fresh.
- Any change to Hermes core. If a hook is missing, widen the generic plugin surface in the
  adapter's own repo first and raise it upstream separately.
- `/whoami` style ACL introspection; the plugin auth hooks cover authorization (section 7).
- Vendoring RNS/LXMF source in-tree; see section 11 for the licence reasoning.

## 10. Test seams

The adapter is testable without a live mesh if the transport is behind a seam:

- **Transport interface**: a protocol with `send_to(destination_hash, payload)`,
  `register_delivery_callback`, `register_failed_callback`, and a `start()/stop()` pair. The
  real implementation wraps LXMF; tests use a fake.
- **Identity mapping**: `get_chat_info` returns name and type for a known hash, a fallback for
  an unknown one, and never raises.
- **Send result mapping**: `DELIVERED` to `success=True`; `FAILED` to `success=False` with
  `error_kind`; `SENT` (propagated) to `success=True` plus an entry in the pending set; packet
  refusal to `retryable=True`.
- **Inbound bridging**: a callback pushed from a foreign thread reaches `handle_message`
  exactly once, and the drain task ends cleanly on `disconnect()`.
- **Lifecycle**: `connect()` twice is idempotent; `disconnect()` then `connect()` works;
  `disconnect()` with no connect does not raise.

Tests exercise the real delivery path with a fake transport, per the gateway's own guidance.
No test asserts on platform lists or command counts.

## 11. Packaging: dependency delivery and instance sharing

Two separate concerns. The first draft invented a manual switch for the second one, which RNS
already performs on its own.

**Dependency delivery: version pin, not vendored source.**

`pyproject.toml` declares `rns` and `lxmf` as pinned versions, currently as ranges
(`rns>=1.5.4,<2.0`, `lxmf>=1.1.1,<2.0`). Tighten these to exact pins for releases so that a
published version of the adapter always runs the RNS/LXMF it was tested against, and upgrade
by bumping the pin and cutting a release.

The alternative, copying RNS/LXMF source into this repository, is rejected on licence
grounds. Both are under the Reticulum License, which is MIT-shaped but carries two added
conditions: no use in systems able to harm humans, and this one:

> The Software shall not be used, directly or indirectly, in the creation of an artificial
> intelligence, machine learning or language model training dataset, including but not
> limited to any use that contributes to the training or development of such a model or
> algorithm.

Running RNS/LXMF as a dependency is fine. Vendoring their source in-tree is also permitted,
but it places their code inside a public repository that crawlers may take for training
corpora, and that use is the one the licence forbids. We cannot control who crawls a public
repository, so the exposure is not ours to manage. A version pin in `pyproject.toml` contains
none of their code, satisfies reproducibility, and gives users a clean upgrade path: bump the
pin, release, users `pip install -U`.

**Instance sharing: RNS handles it. The adapter does not switch anything.**

RNS has first-class shared-instance support and figures this out itself. `Reticulum.__init__`
accepts `require_shared_instance` and `shared_instance_type`, and `__start_local_interface`
tries to become the shared instance first and falls back to attaching as a client when one is
already running (Reticulum.py:397-451):

- It attempts a `LocalServerInterface` on the local socket (398-419). Success means this
  process IS the shared instance.
- If that raises, it falls back to a `LocalClientInterface` and sets
  `is_connected_to_shared_instance = True` (420-441). This is the path a second program takes
  when `rnsd` or another app already owns the socket. It logs "Connected to locally available
  Reticulum instance".
- If neither works it becomes a standalone instance (443-448).

So a bridge process and an `rnsd`, or the bridge and NomadNet, already share one RNS instance
today without either being told to. The observable states are `is_shared_instance`,
`is_connected_to_shared_instance`, and `is_standalone_instance`, and RNS logs which one
applied.

The adapter therefore does **not** implement attach-or-own logic, and this section does not
ask it to. It constructs `RNS.Reticulum(...)` with whatever flags its config supplies and
lets RNS resolve sharing. What the adapter must do is narrower:

- Pass `require_shared_instance` through from config when a user wants to guarantee they are
  attaching to an existing instance rather than starting another. With it set and no shared
  instance available, RNS raises rather than silently standing alone, which is the correct
  loud failure.
- Report which state RNS landed in, in status output and logs, so "which RNS am I talking to"
  is answerable without reading RNS logs.
- Not fight the sharing model. Two programs on one instance is normal and supported, not a
  condition to detect or avoid.

That corrects a real error in the previous draft: it specified a config-driven manual switch
for behaviour RNS already owns, which would have produced two mechanisms fighting over the
same decision.

## 12. Open questions

- Whether the propagated-delivery pending set should eventually expire entries and emit a
  failure after a chosen horizon. Needs a decision before release; not needed to start.
- Whether `standalone_sender_fn` can work at all for out-of-process cron delivery, since a
  cron job in a separate process needs its own RNS. Note this interacts with section 11: a
  separate process can connect to the same shared instance, so this may resolve itself, but
  the shared instance must be running for it to attach.
- Which exact RNS/LXMF versions to pin for the first release, and how much divergence between
  our pin and the RNS version another program on the same shared instance is running is
  tolerable before we warn. Sharing an instance across differing RNS versions is the case to
  check, since the shared instance is created by whichever process got there first.

## 13. Invariants the adapter must not break

From the gateway's contract, each with a known failure behind it:

A send result is binary: done or failed. No third state may be invented.
`connect()` returning True asserts that inbound will be pushed.
Profile-scoped env reads fail closed and never fall back to `os.environ` under multiplex.
Approval and control commands must bypass both message guards to reach the runner.
Adapter acceptance is at-least-once admission, not proof of delivery.
