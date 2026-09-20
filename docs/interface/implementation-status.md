# Implementation inventory

Reviewed 2026-09-20 against the local working trees of AgentBridge and GrantBridge.
This inventory supersedes statements that only live-provider testing remained.
The target contract is broader than the delivered implementation. On 2026-09-20,
controlled live acceptance passed login, fresh verification, binding, execution,
native continuation, idempotency and Stop for Codex, Claude Code and Cursor.
See provider-acceptance.md for the tested scope and remaining deployment limits.

## Implemented and tested with isolated fixtures

| Surface | Implemented behavior | Material limit |
| --- | --- | --- |
| Login start/status | Durable attempt, owner and request key; detached native login owner; caller restart | POSIX worker; an interrupted native process is not silently restarted |
| Login check | Detached fresh-process check; optional explicit inference probe | Claude local status is loaded_only; activation needs inference=true, which consumes usage |
| Login complete | Verified state required; account binding and observation committed atomically; replayable | Provider identity must match; no silent replacement of an existing account |
| Login cancel | Durable cancellation before binding; late verification cannot resurrect a cancelled attempt | Already authorized provider grants are not remotely revoked by local cancellation |
| Cursor credentials | Encrypted GrantBridge vault to private pipe to execution environment; reference only in AgentBridge | Production Cursor backend; no automatic key renewal; requires cursor-sdk extra |
| Cursor account status | Checks binding, local revocation and expiry on refresh | Cached provider verification; does not make a new remote identity request |
| Account RPC | Safe account projection; arbitrary registration disabled | Administrative SDK/CLI registration is a separate trusted local surface |
| Instances | Explicit account, durable create idempotency, get/list/model update/archive/version checks | No model availability validation, metadata or persistent advanced defaults |
| Text messages | Distinct accepted message and turn IDs; durable admission; detached execution | One active turn per account; no attachments or steering of an active turn |
| Transcript | Bounded message count, numeric offset and role filtering before reconstruction | Snapshot view, not a durable incremental message projection; before unsupported |
| Events | Per-turn and per-instance sequence queries, bounded pages | Store-global sequence gaps between filtered events are normal; no HTTP/SSE/WebSocket server |
| Stop/recovery | Persisted stop; observed worker identity; lost workers marked interrupted | Grace override at stop is unsupported; configure RunOptions.stop_grace before launch; no automatic replay of unknown effects |
| Resume | Explicit new message/turn with native session or portable context | Not the same message_id with multiple attempts; native/reconcile modes are not separate provider workflows |
| Transfer/export | Native version checks, explicit portable fallback, bounded omissions, destination idempotency | File copy and SQLite commit are not one crash-atomic transaction; replay metadata may be incomplete |
| Models | Live Codex app-server catalog when requested, marked static fallback otherwise | Claude and Cursor live catalog readers are not implemented |
| Usage | Provider observations per turn; Codex account reader; stored observation history | Claude/Cursor account readers, time filters and aggregation are incomplete |
| Errors | Structured error envelope; unknown outcome never retryable; safe adapter messages | Provider-native error classification is incomplete; compatibility codes remain |

## Explicitly unsupported or unfinished

- Interactive permission response delivery. permissions.respond now refuses the
  operation instead of recording a response that the provider never receives.
- Images, other attachments, context-window selection, metadata/provider_options,
  persistent effort/tool/sandbox defaults and changing these defaults on instances.
- Live model enumeration for Claude and Cursor; model entitlement verification at
  instance creation. A static entry has unknown availability, not proof of access.
- Full account usage/quota readers for Claude and Cursor, historical time filters,
  quota aggregation and uniform paginated collection envelopes.
- Shared message identity across execution retries, active-turn message steering,
  comprehensive transfer crash recovery and retention/deletion operations.
- Complete mapping of provider authentication, quota and permission failures into
  the target error catalogue. Generic provider_failed remains possible.
- Network transports. This build delivers Python SDK, CLI and JSON-RPC over stdio.
  A future transport should share the same dispatcher and event store.
- A Fullbrain client end-to-end deployment test and an unassisted mobile Claude
  login. The bounded live-provider acceptance described above has passed.

## Evidence and integration boundary

- tests/test_auth_runtime.py exercises caller exit, durable replay, asynchronous
  verification, cancellation, atomic binding, Cursor worker injection, redaction
  and expired credentials. Its provider and SDK are deterministic test doubles.
- tests/test_contract_regressions.py covers pagination, long event histories,
  unknown outcomes, partial-line timeouts and refusal of ambient Cursor credentials.
- tests/test_grantbridge.py covers identity binding and the real local stdio
  adapter when a GrantBridge checkout is available.
- GrantBridge test/agentbridge-credentials.test.mjs exercises its actual encrypted
  vault, private credential pipe, restart idempotency and coordinator isolation.
- Real Cursor acceptance found missing backend metadata in the SDK login result.
  GrantBridge now persists the explicitly selected backend; fresh authorization,
  private credential resolution and execution subsequently passed.

Fullbrain can integrate the bounded local contract in a controlled environment.
Production enablement still requires a pinned pair of repository revisions,
installation of the optional SDK/native CLIs, live acceptance for the selected
provider, restart/stop tests through the actual Fullbrain client and an agreed
supported feature subset. General production readiness is not established.
