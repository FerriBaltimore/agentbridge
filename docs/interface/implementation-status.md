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
| Text messages | Distinct accepted message and turn IDs; durable admission; detached execution | One active turn per account; no steering of an active turn |
| Attachments | Bounded inline text and images for all three engines; durable idempotency and safe descriptors | No path/URL inputs or PDFs; portable transfer explicitly omits attachment content |
| Interactive permissions | Native Codex/Claude request and one-use allow/deny delivery; expiry, replay and Stop | Cursor SDK has no host response channel; no session-wide grants |
| Transcript | Bounded message count, numeric offset and role filtering before reconstruction | Snapshot view, not a durable incremental message projection; before unsupported |
| Events | Per-turn and per-instance sequence queries, bounded pages | Store-global sequence gaps between filtered events are normal; no HTTP/SSE/WebSocket server |
| Stop/recovery | Persisted stop; observed worker identity; lost workers marked interrupted | Grace override at stop is unsupported; configure RunOptions.stop_grace before launch; no automatic replay of unknown effects |
| Resume | Explicit new message/turn with native session or portable context | Not the same message_id with multiple attempts; native/reconcile modes are not separate provider workflows |
| Transfer/export | Native version checks, explicit portable fallback, bounded omissions, destination idempotency | File copy and SQLite commit are not one crash-atomic transaction; replay metadata may be incomplete |
| Models | Live paginated Codex, Claude initialize and Cursor SDK catalogs; observed effort, parameters/variants and context metadata | Catalogue membership does not verify model entitlement; absent sizes remain unknown |
| Usage | Normalized windows, countdowns, scoped Claude limits and Codex spend pools/reset credits; explicit durable Codex reset redemption | Cursor SDK account quota unavailable; no historical time filters or aggregation; reset mutation tested with fixtures |
| Errors | Structured native failures, safety blocks, quota/auth/billing/context/output limits, cut streams and safe terminal outcomes; durable unknown cases and reviewed exact classifications | Diagnostic backend requires a separate explicit Cursor account; structural validation does not verify meaning; incomplete evidence stays unknown |

## Explicitly unsupported or unfinished

- Cursor interactive permission responses and account quota: the supported SDK
  exposes neither host approval delivery nor remaining account capacity. These
  are explicit provider limits, not simulated successes.
- PDFs/other binary attachments, context-window selection, metadata/provider_options,
  persistent effort/tool/sandbox defaults and changing these defaults on instances.
- Model entitlement verification at instance creation. Missing catalogue metadata
  remains unknown; unavailable discovery returns no invented model entries.
- Historical usage time filters, quota aggregation and uniform paginated
  collection envelopes.
- Shared message identity across execution retries, active-turn message steering,
  comprehensive transfer crash recovery and retention/deletion operations.
- Exhaustive future provider error mapping. Unknown failures are captured with
  hashes and fixed vocabulary, without their original bodies. A separate AI
  session can propose a classification; only explicitly reviewed rules become
  active. Uncertain meanings stay unknown. See [error learning](../error-learning.md).
- Network transports. This build delivers Python SDK, CLI and JSON-RPC over stdio.
  A future transport should share the same dispatcher and event store.
- A completely fresh mobile Claude login after the scroll fix. The corrected
  viewer passes the scroll-and-dismiss regression on a physical Galaxy; this
  does not repeat the earlier real-provider authorization journey.

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

The permission, attachment and catalogue subset is specified in
[interactive-inputs.md](interactive-inputs.md). Fullbrain implementation is
outside this repository and outside the current delivery scope.

The normalized quota, reset, model and failure subset is specified in
[usage and failures](../usage-and-failures.md), including evidence boundaries.

The repository-wide metadata and structural compatibility audit is recorded in
[provider contracts](../development/provider-contracts.md). Published fields,
native compatibility formats and local policy constants are distinct categories.
