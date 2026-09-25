# AgentBridge protocol

AgentBridge exposes the Python SDK, CLI and JSON-RPC 2.0 over stdin/stdout.
Each RPC request has `jsonrpc`, `id`, `method` and object `params`.
Notifications omit `id` and receive no response. Errors carry a stable
`data.code`. The target contract is in [interface/README.md](interface/README.md).

## One account and execution flow

`accounts.login.start` asks GrantBridge to start browser OAuth through a
dedicated local CLIProxyAPI sidecar. GrantBridge coordinates the login;
CLIProxyAPI stores and refreshes the upstream credential. AgentBridge stores
only safe attempt evidence and references to the proxy URL and key environment
variables. `accounts.login.status` polls the attempt, `accounts.login.check`
verifies one active upstream identity and its model IDs at the proxy, and
`accounts.login.complete` atomically creates or reauthenticates the account.
`accounts.login.cancel` cancels a pending OAuth session explicitly. If OAuth
already completed, cancellation reports `already_finished`; it never claims
the saved credential was deleted. If the proxy no longer knows an OAuth state,
AgentBridge records an interrupted attempt. Inspect the sidecar for a
credential left by the uncertain attempt, explicitly abandon the local
attempt, and use a fresh dedicated sidecar endpoint for a retry. Abandoning
locally does not claim that the remote OAuth session was cancelled.

Every executable account uses Codex as its engine and the local proxy as its
endpoint. `provider` records the upstream account type (`codex`, `claude` or
`grok`). Direct account registration and direct provider execution are closed.
Historical account records can be inspected but cannot start a new turn.

`models.list` aggregates configured model IDs and separately marks IDs with
fresh, verified proxy observations.
`instances.create` selects an eligible account for an exact model ID, or pins
the given `account_ref` after verifying it. Automatic routing can select a new
account between turns. It does not change accounts or replay an unknown turn
while the turn is running. One conversation binds once to one native Codex
session. Model and upstream route changes resume that session and its history;
missing or divergent native state fails explicitly without a new thread.

## Records and evidence

An `Account` identifies one upstream identity and one dedicated proxy route.
A `Session` binds a conversation to a workspace, selected model, account and
optional parent, with an immutable native Codex session after its initial
binding. The model and upstream route may change between turns. A `Run` is an
admitted turn with a distinct `message_id`.
Request keys make admission idempotent across restarts. The SQLite store allows
only one active run per session or account.

Events have a monotonic store sequence, run and session ID, kind, timestamp
and JSON data. A `tool_result` with `outcome: unknown` means that AgentBridge
did not observe the result of requested work; it is never counted as success.
A `gap` records unparsed provider output. A `subagent` event records only
observed child state and cannot prove hidden work completed. Context export
retains unresolved outcomes and lists omitted evidence explicitly.

## Main operations

| Method | Purpose |
| --- | --- |
| `capabilities` | Declare the fixed Codex proxy route and verification limits. |
| `accounts.list/status/usage/usage_history` | Read configured references and persisted proxy observations. |
| `accounts.login.start/status/check/complete/cancel` | Manage the single GrantBridge and local proxy account flow. |
| `models.list` | List exact model IDs observed or configured for proxy routes. |
| `instances.create/get/list/transfer/export` | Manage conversations bound to one Codex session; explicitly export or fork bounded portable context. |
| `messages.create/list` | Submit and inspect messages. |
| `turns.get/list/events/stop` | Inspect or explicitly cancel a turn. |
| `recover` | Mark a lost worker interrupted without rerunning it. |
| `error_cases.list/get` | Read safe, deduplicated execution failures. |
| `error_proposals.create/get/validate`, `error_rules.get/activate/deactivate` | Review and activate scoped failure classifications. |

The earlier `accounts.quota.reset`, direct diagnosis and native account quota
paths are unsupported in v2. See [implementation status](interface/implementation-status.md),
[usage and failures](usage-and-failures.md) and [error learning](error-learning.md)
for the current boundaries.
