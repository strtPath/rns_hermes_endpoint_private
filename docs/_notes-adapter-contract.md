# Gateway Platform Adapter Contract Notes

(incremental notes; append after every 2-3 file reads)

## 1. The minimum contract

Base class: `BasePlatformAdapter` at `gateway/platforms/base.py:1821` (file is 4678 lines).
It is an `ABC` (`@abstractmethod` on four methods).

The four `@abstractmethod` methods:

1. `connect(self) -> bool` (base.py:2594)
   - Must connect to the platform and start its inbound listener loop.
   - Returns `True` on success, `False` on failure.
   - The gateway's `GatewayRunner` (via `gateway/run_adapters.py::_create_adapter`) calls this
     during startup and on reconnect.

2. `disconnect(self) -> None` (base.py:2600)
   - Must stop the listener, close connections, and cancel any in-flight tasks.

3. `send(self, chat_id: str, text: str, ...) -> SendResult` (base.py:2604)
   - Must deliver a single text message to a chat.
   - Returns a `SendResult` (a small dataclass in `base.py`).
   - The gateway's send-message tool, cron delivery, and inline-reply paths all call this.

4. `get_chat_info(self, chat_id: str) -> dict` (base.py:4602)
   - Must return a dict with at least `{name, type, chat_id}` for a chat.
   - Used by the channel directory and status display.

Inbound delivery (how a message gets TO the gateway):

The adapter receives a raw platform message, normalizes it into a `MessageEvent`
(`gateway/platforms/event.py:36`), and calls `self.handle_message(event)`.
`handle_message` is defined on the base class at `base.py:3890`.

`handle_message` spawns a background task (so new messages can arrive while an agent runs)
and eventually calls `self._message_handler(event)`.
`_message_handler` is an instance attribute set by the gateway via:
`adapter.set_message_handler(handler)`  (`base.py:2230`).

The gateway calls `set_message_handler` from `gateway/run_adapters.py:1147`:
```
adapter.set_message_handler(message_handler or self._primary_message_handler())
```
This is the single wiring point. The handler is the gateway's own dispatch function
that runs the agent turn, streams tool progress, and delivers the final reply via `adapter.send()`.

So the MINIMUM working adapter to receive a message and reply:
- `__init__` calling `super().__init__(config, Platform.YOUR_PLATFORM)`
- `connect()` that starts the listener loop
- `disconnect()` that stops it
- `send(chat_id, text, ...)` that delivers one message
- `get_chat_info(chat_id)` returning `{name, type, chat_id}`
- In the listener loop: build a `MessageEvent`, call `self.handle_message(event)`

That's it. Everything else is inherited.

## 2. The declaration flags

All class-level flags on `BasePlatformAdapter` (base.py:1826-1865):

| Flag | Default | Meaning / what changes |
|------|---------|------------------------|
| `supports_code_blocks` | `True` | Whether the platform renders fenced code blocks. Controls whether the gateway formats tool output / final reply with markdown code fences. |
| `supports_status_text` | `True` | Whether the platform supports a "typing…" / status text channel. Controls whether the gateway emits status updates (e.g. "running tool X") to the user. |
| `supports_async_delivery` | `True` | Whether async-delivery tools (e.g. background jobs that finish later) can promise to deliver a result to this platform. Webhook and API-server adapters set `False` because they are stateless request/response. |
| `splits_long_messages` | `True` | Whether the gateway should split replies exceeding `MAX_MESSAGE_LENGTH` into multiple sends. |
| `typed_command_prefix` | `"/"` | The prefix that marks a command message (e.g. `/new`). Used by `MessageEvent.is_command()` and the gateway's command dispatcher. |
| `supports_inchannel_continuable` | `False` | Cron jobs delivered to this platform can continue in-channel (flat reply continues the turn). `False` fails safe to threaded delivery. |
| `interactive_resume` | `True` | See below. |
| `serves_profile_prefix` | `False` | Port-binding adapter that answers `/p/<profile>/...` for every served profile under `gateway.multiplex_profiles`. HTTP-inbound adapters (webhook, api_server) set `True`. |

`interactive_resume` (base.py:1852-1860) is the most important flag:

```
# A human can answer "session restored - what next?"; webhook-style platforms set False so
# auto-resume finishes the work instead of asking nobody.
# The startup auto-resume turn (_schedule_resume_pending_sessions -> the _is_resume_pending
# branch in _handle_message_with_agent) reads this to pick its guidance: interactive platforms
# (Telegram, Slack, Discord DMs, ...) get "report the restore and ask what the user wants next";
# non-interactive event platforms (webhook) get "finish the interrupted work" because nobody is there to
# answer, and an acknowledgement would silently abandon the task (#57056). Read generically via
# getattr(adapter, "interactive_resume", True) - no per-platform branching at the call site.
```

When `interactive_resume` is `True` (default, e.g. Telegram, Discord, Slack):
  The gateway's startup auto-resume turn gets the guidance "report the restore and ask what
  the user wants next." The agent produces a "session restored" acknowledgement and waits for
  the user to say what to do next.

When `interactive_resume` is `False` (webhook, api_server):
  The gateway's startup auto-resume turn gets the guidance "finish the interrupted work."
  Nobody is there to answer an acknowledgement, so the agent completes the work that was
  interrupted rather than asking.

## 3. A small reference implementation

`gateway/platforms/tcp_site.py` (59 lines, 2992 bytes) is the smallest in-tree adapter.

What it implements:
- `TCP_SITE_PORT = 8765` module constant
- `TCP_SITE_HOME_CHANNEL = "tcp:site"` module constant
- `TCP_SITE_DEFAULT_NAME = "TCP Site"` module constant
- `class TCPAdapter(BasePlatformAdapter)` with:
  - `platform = "tcp_site"` (overrides the class attr)
  - `__init__(self, config)` that calls `super().__init__(config, Platform.TCP_SITE)`
  - `async def connect(self) -> bool`: creates an `asyncio.start_server` on 127.0.0.1:8765,
    accepts connections, reads newline-delimited lines, and for each line builds a
    `MessageEvent` and calls `self.handle_message(event)`.
  - `async def disconnect(self)`: cancels the server task.
  - `async def send(self, chat_id, text, ...) -> SendResult`: writes the text + newline to the
    client's `asyncio.StreamWriter`.
  - `async def get_chat_info(self, chat_id) -> dict`: returns `{"name": "TCP Site", "type": "dm", "chat_id": chat_id}`.

It inherits from `BasePlatformAdapter` and does NOT override any of the declaration flags
(they all keep their defaults). It does NOT implement `send_typing`, `send_image`,
`send_document`, or any interactive UX methods (buttons, clarify, approval prompts).

This proves how small an adapter can be: four abstract methods plus the `handle_message`
call in the listener loop, and you get session state, tool streaming, clarify round-trip,
approval prompts, slash commands, and session persistence for free.

Second data point: `gateway/platforms/api_server.py` (class `APIServerAdapter` at line 1152,
inherits `OpenAICompatRoutesMixin, BasePlatformAdapter`). It is a stateless request/response
HTTP server routing OpenAI-format requests through the agent. It sets:
- `supports_async_delivery = False` (line 1157)
- `serves_profile_prefix = True` (line 1159)
- `interactive_resume = False` (line 1163)
It does NOT override `supports_code_blocks` or `supports_status_text` (keeps defaults).
This is an adapter with no human on the other end: the client is an API consumer, not a
chat user, so it cannot receive async deliveries, cannot show status text, and should not
ask "what next?" on resume.

## 4. What the gateway provides the adapter

Registration and instantiation:

- `gateway/platform_registry.py` (389 lines) is the standalone registry module.
- `gateway/run_adapters.py::_instantiate_adapter` (line 1657) is the factory:
  it checks the plugin registry first, then falls back to built-in adapters in
  `gateway/run.py::_instantiate_builtin_adapter`.
- `gateway/run_adapters.py::_create_adapter` wraps the factory and binds every successful
  adapter to its `GatewayRunner` (sets `adapter.gateway_runner = runner` and calls
  `set_message_handler`, `set_session_store`, `set_busy_session_handler`, etc. at line 1147).

How a platform name gets to an adapter class:

In-tree (built-in): the `Platform` enum in `gateway/config.py` has one member per platform.
`gateway/run.py::_instantiate_builtin_adapter` is a long `if/elif` chain keyed on
`Platform.*` that imports and constructs the adapter class.

Out-of-tree (plugin): `gateway/platform_registry.py` exposes a registry. A plugin's
`register(ctx)` entry point calls `ctx.register_platform(name, label, adapter_factory,
check_fn, validate_config, is_connected, ...)` to register its adapter. The gateway's
`_instantiate_adapter` checks the plugin registry FIRST (before built-ins), so an
out-of-tree plugin can shadow or add a platform without touching core code.

Evidence that out-of-tree registration works:

- `gateway/platform_registry.py` exists as a standalone module (389 lines, 23040 chars).
- `gateway/run_adapters.py:1658` docstring: "Instantiate the adapter for a platform:
  plugin registry first, then built-ins."
- `plugins/platforms/matrix/adapter.py` (103908 chars) is a full adapter living under
  `plugins/` (NOT under `gateway/platforms/`), registered via `ctx.register_platform`.
- `plugins/platforms/a2a/__init__.py:89` calls `ctx.register_platform(name="a2a", ...)`.
- `plugins/platforms/whatsapp/adapter.py:1027` calls `ctx.register_platform(name="whatsapp", ...)`.
- `plugins/platforms/buzz/adapter.py:1989` calls `ctx.register_platform(name="buzz", ...)`.
- `ADDING_A_PLATFORM.md` (lines 5-11) documents the plugin path: "Create a plugin
  directory in `~/.hermes/plugins/` (or under `plugins/platforms/` for bundled plugins)
  with a `plugin.yaml` and `adapter.py`. The adapter inherits from `BasePlatformAdapter`
  and registers via `ctx.register_platform()` in the `register(ctx)` entry point.
  This requires zero changes to core Hermes code."

So: YES, out-of-tree registration is supported and is the RECOMMENDED path for
community/third-party adapters. A Reticulum adapter could live in
`plugins/platforms/reticulum/` and register via `ctx.register_platform` without
touching any core file.

## 5. The machinery inherited

Capabilities an adapter gets WITHOUT implementing them (all in the gateway / base class,
NOT in the individual adapter):

1. Per-session conversation state
   - File: `gateway/run_adapters.py` (session store, session key derivation) +
     `gateway/platforms/base.py` (`_event_session_key`, `_source_session_key`, session
     guard, pending message queue).
   - Lives in: GATEWAY (shared). The adapter calls `self.handle_message(event)` and the
     gateway handles session routing, key derivation, and persistence.

2. Tool-call / step streaming to the user
   - File: `gateway/run_turn_runner.py` (sets `agent.tool_progress_callback`,
     `agent.tool_start_callback` at lines 1296-1297; the turn runner emits step
     events). The base adapter's `_process_message_background` method (base.py:4380+)
     forwards these to the platform via `adapter.send()` / status text.
   - Lives in: GATEWAY (shared). The adapter does NOT implement tool streaming.

3. The clarify round-trip
   - File: `gateway/run_turn_runner.py:1289` sets `agent.clarify_callback = self._clarify_callback_sync`.
     The `_clarify_callback_sync` method (line 1341) presents a clarify prompt and blocks
     on a response. It schedules `send_clarify` on the gateway loop, blocks on a
     threading.Event with a timeout, and returns the response string.
   - Why an adapter gets working clarify for free: the clarify callback is set on the
     AGENT (not the adapter). The gateway's turn runner owns the callback. The adapter's
     only role is to (a) call `send_clarify` when the gateway asks it to render buttons
     (optional; degrades to text), and (b) route inbound button taps to
     `tools.clarify_gateway.resolve_gateway_clarify`. The base class provides a default
     text-based `send_clarify` that works on any platform without override.
   - Lives in: GATEWAY (shared). The adapter does NOT implement the clarify round-trip.

4. The approval prompts
   - File: `gateway/run_turn_runner.py` (approval flow) + `base.py` (`_send_exec_approval_prompt`
     template method at base.py, builds the shared text via `_format_exec_approval` and the
     choice set from `prompt.actions`). Inbound dispatch routes to
     `tools.approval.resolve_gateway_approval`.
   - Lives in: GATEWAY (shared). The adapter only maps choice rows to native widgets if it
     wants interactive buttons; text fallback is provided by the base class.

5. Slash commands
   - File: `gateway/run.py` / `gateway/run_adapters.py` (command dispatcher, reads
     `typed_command_prefix`, routes `/new`, `/reset`, `/stop`, etc.). `base.py` has
     `_dispatch_inline_reply` (line 3456) and the command-guard / session-command paths.
   - Lives in: GATEWAY (shared). The adapter does NOT implement slash command parsing.

6. Memory / skills
   - File: `gateway/run_turn_runner.py` (sets `agent.memory_notifications` at line 1288;
     the turn runner loads memory and skills before the agent turn).
   - Lives in: GATEWAY (shared). The adapter is unaware of memory/skills.

7. Session persistence
   - File: `gateway/run_adapters.py` (session store, `set_session_store` at line 1149).
   - Lives in: GATEWAY (shared). The adapter does NOT persist sessions.

Summary: ALL of these live in the gateway (shared machinery). The adapter's job is
strictly: (a) connect to the platform, (b) normalize inbound messages into
`MessageEvent`s and call `self.handle_message(event)`, (c) deliver outbound messages via
`send()`, (d) return chat info via `get_chat_info()`. Everything else is inherited.
