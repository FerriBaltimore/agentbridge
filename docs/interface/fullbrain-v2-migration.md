# Fullbrain v2 migration to AgentBridge v2

This is a handoff for the current AgentBridge implementation, not a claim that
Fullbrain already runs it. Read the [RPC examples](fullbrain-v2-rpc.md),
[implementation inventory](implementation-status.md) and
[provider acceptance gate](provider-acceptance.md) together. Pin a reviewed,
immutable AgentBridge, GrantBridge and CLIProxyAPI version set before changing
Fullbrain's worker. A method in the target [operations](operations.md) list is
not evidence that every listed optional parameter is usable.

## Compatibility decisions

| Fullbrain v1 assumption | AgentBridge v2 contract | Fullbrain change |
| --- | --- | --- |
| `contract_version == "v1"`; pass `engine` to discovery | Require `"v2"`; `execution_engine` is always `codex`; discovery rejects `engine` | Replace the version gate and request builder; keep provider as upstream account data |
| Choose among execution engines | Choose an exact model from `models.list`; upstream providers are `codex`, `claude`, `grok` | Replace engine tabs and engine-specific settings with model controls returned by the SDK |
| Bind every chat to `engine_account_ref` | Create an automatic instance by model, optionally filtered by `provider`; `account_ref` explicitly pins an account | Store routing policy separately from the actual account selected for each turn |
| A chat's account never changes | Automatic routing can change account between turns | Render the actual `messages.create.account_ref`, `turns.get.account_ref` and `route.selected` evidence; preserve a stable route during one turn |
| Request per-engine model catalogue | `models.list` aggregates exact IDs and candidate/observed account refs | Remove `engine` from the API call; treat configured and observed models as routing evidence, not entitlement |
| Native account login, device or callback relay | One GrantBridge-mediated local CLIProxyAPI browser flow for every new account | Use `accounts.login.start/status/check/complete/cancel`; no direct provider login |
| Codex-specific quota parser | `accounts.usage`/`usage.get` return source, support, staleness, reason and optional `quota_windows` | Show missing usage as unknown; keep turn tokens separate from account quota |
| Fullbrain sends `context_package` and `mcp` on `messages.create` | AgentBridge v2 validates bounded packages and a private Unix-socket MCP descriptor | Upgrade the pinned SDK and test native app-server acceptance plus Fullbrain sandbox and facade revocation before claiming parity |

The [context and MCP contract](context-and-mcp.md) has deterministic fixture
coverage. Fullbrain must pass its selected package and operation-bound facade
unchanged, then verify actual Codex app-server acceptance and sandbox isolation
inside the worker. Fixture success alone does not establish chat parity.

## Fullbrain code touchpoints

These paths are in the Fullbrain repository as inspected on 2026-09-23. They
identify work to review, not instructions for AgentBridge to edit that repo.

| Path | Current dependency to replace or preserve |
| --- | --- |
| `backend/fullbrain/adapters/agentbridge/client.py` | v1 artifact pin and contract check; `context_package` and `mcp` submission |
| `backend/fullbrain/adapters/agentbridge/process.py` | v1 startup check and isolated worker mounts, network and executable list |
| `backend/fullbrain/adapters/agentbridge/model_catalog.py` | Engine-scoped model lookup and response validation |
| `backend/fullbrain/adapters/agentbridge/login.py` and `login_callbacks.py` | Engine/device/callback branches versus one local proxy login flow |
| `backend/fullbrain/adapters/agentbridge/account_usage.py` | Provider-specific quota mapping versus proxy account usage |
| `backend/fullbrain/application/chat/engine_preference.py` and `send_message.py` | Required engine/account/model binding and per-turn account assumptions |
| `backend/fullbrain/storage/migrations/001_initial.sql` and `061_chat_engine_preferences.sql` | Durable engine account binding and preference schema |

## Host data and API migration

1. Preserve existing v1 conversations, receipts, account references and audit
   evidence as historical data. Do not reinterpret a v1 `engine_account_ref`
   as a v2 proxy account or silently resume a v1 instance on another account.
2. Add a v2 conversation routing policy: exact model, automatic or pinned mode,
   optional provider filter, and optional pinned `account_ref`. Keep the
   selected account as **turn evidence**, not the conversation's immutable
   owner. A provider filter applies when the instance is created; changing that
   policy requires a new instance or an explicit linked successor.
3. Keep the Fullbrain tenant boundary around one private AgentBridge state root
   and worker. Do not share that root, its supervisor or its local sidecars
   across Fullbrain users. Keep Fullbrain's user-visible account authorization
   checks before calling AgentBridge, including for an explicit pin.
4. Migrate Fullbrain's request and response validators together. In particular,
   `accounts.list`, event reads and usage history return arrays, while
   `models.list` has `items`, `next_cursor` and `has_more`. Do not assume one
   universal collection envelope.
5. Store `instance_id`, `message_id`, `turn_id`, idempotency keys and the last
   committed event `seq`. Commit each event page before moving the cursor.
   `message.completed` can carry `final: true`; use `run.finished` or
   `turns.get.state` to determine that the **turn** ended.
6. Keep v1 and v2 AgentBridge state roots separate, with a verified backup of
   the v1 root and a Fullbrain database migration that preserves old records.
   An old v1 artifact must never open a v2-mutated SQLite root. A rollback
   selects the old pinned artifact and old conversation cohort explicitly;
   v2-created conversations remain read-only until v2 is restored. Restore
   storage only from a tested backup, not by assuming a schema downgrade.
   Reauthorize new proxy accounts through v2 instead of copying credentials.

The Fullbrain v1 backend currently enforces an immutable account binding and
sends `context_package`/`mcp`. Its worker also checks the v1 contract. Those
parts require code and schema changes in Fullbrain; switching a wheel alone
cannot migrate the application.

## Worker and authentication deployment

Install the reviewed AgentBridge wheel in Fullbrain's **own** isolated worker
environment. Make the compatible CLIProxyAPI executable available on `PATH` or
set `AGENTBRIDGE_CLIPROXY_BIN`. Supply the GrantBridge adapter through the
reviewed installation; set `AGENTBRIDGE_GRANTBRIDGE_ROOT` only if discovery
cannot find it. AgentBridge's managed sidecars require Linux `memfd` and a
dedicated loopback endpoint and auth directory for each account. Their keys
are generated by the supervisor and delivered over private local channels to
AgentBridge and its workers as needed. The client key reaches Codex; the
management key does not. Fullbrain stores only safe public references, and
key values do not enter the AgentBridge database or public projections.

Fullbrain's worker sandbox must permit the sidecar executable, its private
storage, loopback communication with Codex and GrantBridge, and the outbound
connections required by the selected provider. Test these inside the actual
worker namespace. A sandbox that unshares networking or mounts only the old
provider binaries will fail even if a standalone AgentBridge smoke test passes.

The implemented login flow supports `mode="browser"` and
`browser="same_host"` only. Fullbrain's remote browser/callback journey is
**not yet supported** by this contract. Keep remote account creation gated
until an explicit remote browser flow is implemented and accepted. A displayed
authorization URL by itself does not verify or create an account.

## Turn behavior and recovery

Create a v2 instance with `model` and `workspace_path`. Omit `account_ref` for
automatic routing; optionally set `provider` to limit candidates. A pinned
instance supplies `account_ref` and cannot also supply `provider`. The routing
policy is fixed on that instance, and automatic selection occurs before every
admitted turn. Send supported effort, numeric `context_window`, permissions,
sandbox and timeout **per turn** with `messages.create`. Advanced instance
defaults, `allowed_tools`, `max_budget`, `provider_options` and arbitrary
metadata are not usable in the current adapter.

Record the accepted turn's actual account. `route.selected` contains route
evidence including `account_changed`, `portable_context_used` and
`context_omitted_count`. Surface omissions when account changes. A model shown
as `proxy_observed` only has local catalogue evidence; live entitlement is
still unverified. A missing quota window is unknown capacity, never free
capacity.

After a disconnect, inspect the same instance, turn and event cursor. Reuse
the original idempotency key when resubmitting a request whose acceptance is
uncertain. Do not automatically rerun a turn with an unknown outcome, transfer
it to another account, or replay its tools. Stop is an explicit cancellation.
The host decides whether a new turn is appropriate after inspecting recovery.

## Acceptance before enabling a provider

- Run AgentBridge's deterministic tests, repository guard, wheel install and
  installed CLI/RPC smoke test with isolated state. These establish local
  implementation behavior, not live provider acceptance.
- Test Fullbrain's pinned worker with its real sandbox: startup, version gate,
  model/account/usage reads, login attempt ownership, account deletion, event
  cursor restart, permissions, Stop and uncertain recovery.
- Test the `context_package`/MCP bridge and remote browser journey inside the
  Fullbrain worker. Missing capabilities must fail visibly rather than
  disappearing from a request.
- For each enabled provider and model, use a disposable authorized account to
  record live OAuth, identity binding, one turn, usage availability, tool
  behavior and restart. Test automatic account changes separately. Fixture
  success or a catalogue entry must not be reported as live acceptance.
- Keep a v1 historical read path, separate state roots and a verified restore
  path. Confirm that old conversations are not rebound or resumed through v2
  during rollout or rollback.

AgentBridge does not modify Fullbrain's installation or data. Fullbrain's team
owns its schema, UI, worker and rollout changes.
