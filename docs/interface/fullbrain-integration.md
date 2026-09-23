# Fullbrain integration

For a Fullbrain v1 adapter, start with the
[v2 migration guide](fullbrain-v2-migration.md) and
[implemented RPC examples](fullbrain-v2-rpc.md). This page describes the host
loop after its migration gates are met; it does not certify Fullbrain feature
parity or live provider acceptance.

Fullbrain should treat AgentBridge as a local execution kernel over JSON-RPC
stdio. It owns the model picker, permissions, product retries and UI.
AgentBridge owns automatic account selection for v2 proxy instances, provider
processes, durable execution state and normalized observations. Fullbrain must
not call GrantBridge or a provider CLI directly.

## Startup

1. Start one AgentBridge process for the Fullbrain worker and give it a private
   state directory.
2. Call `capabilities.get` and retain the contract version and maturity fields.
3. Call `models.list` for the model picker. Treat configured
   model support as routing information, not verified entitlement. Use
   `accounts.list` to show safe account labels and observed status.
4. Call `instances.create` with the selected model and an idempotency key.
   Supply `account_ref` only when the user explicitly pins a configured account.

Persist `instance_id` and `routing_mode`. An automatic instance reports its
current `account_ref`; it may change between turns. Each accepted turn records
its actual account route. A pinned instance stays on the requested account.

## Conversation loop

Call `messages.create` with an idempotency key. Persist both returned IDs. Poll
`instances.events(instance_id, after_seq, limit)` and advance the Fullbrain
cursor only after committing each page. Translate normalized events to the UI.
Use `turns.get` for a complete turn and `turns.events` when a single-turn view
is needed. Do not use `follow=true` in the first integration.
Record the account on each accepted turn and display route changes when useful.
AgentBridge fixes one route for the entire turn, including tool calls. On an
automatic account switch it starts a fresh Codex thread with bounded portable
context and reports omissions; Fullbrain should surface those omissions.
The current v2 `messages.create` does not accept Fullbrain's existing
`context_package` or `mcp` fields. Fullbrain must resolve those requirements
through a reviewed contract before claiming rules, skills, tools or evaluation
isolation parity. Do not silently drop the fields during migration.

On reconnect, reopen the same instance and cursor. A timeout or disconnect is
not permission to submit the message again. Reuse the same idempotency key and
inspect the returned `replayed` flag. If a turn ends with `unknown_outcome`,
show that state and require an explicit recovery decision.

## Authentication

Call `accounts.login.start` with `provider` and `name`. AgentBridge prepares an
empty, dedicated local CLIProxyAPI sidecar; the deployment must provide a
compatible CLIProxyAPI executable on `PATH` or via
`AGENTBRIDGE_CLIPROXY_BIN` on a Linux host with `memfd` support. Set
`AGENTBRIDGE_GRANTBRIDGE_ROOT` when the GrantBridge adapter is not discoverable
beside the source checkout. Persist the returned `attempt_id` and `owner_ref`;
show the authorization URL or user code. GrantBridge coordinates the browser
flow through the sidecar Management API, while CLIProxyAPI owns and refreshes
the upstream credential in its isolated auth directory. Poll
`accounts.login.status`, then call `accounts.login.check` and
`accounts.login.complete`. Completion repeats the identity and model checks;
a browser callback alone never creates an account.
Resume an attempt after restart with the saved references, or cancel it with
`login.cancel`. For an externally managed proxy, advanced callers may also
provide `proxy_base_url`, `key_env` and `management_key_env` together to the
same login flow. These key arguments are environment variable names, not
values. The management reference is required for pinned and automatic routes
and is never sent to Codex. Managed key values pass over private local
channels during login and execution and never enter the
AgentBridge database. The initial browser flow is
same-host; real OAuth acceptance is pending. A remote Fullbrain browser cannot
complete the current flow without a separately implemented and accepted remote
browser path.

## Errors and capability gates

Branch on `error.data.code`, `category`, `phase`, `outcome` and `retryable`, not
on message text. Never automatically retry `unknown_outcome`. A capability is
usable only when `support` is not `unsupported` and its maturity meets the
deployment gate. Fixtures and controlled live acceptance are recorded in
provider-acceptance.md; the actual Fullbrain deployment must verify its selected
provider, version and supported feature subset separately.
The v2 CLIProxyAPI path has local fixture acceptance only. Provider model
entitlement, quota telemetry and cross-account continuation remain separate
live acceptance requirements. Unknown quota must remain unknown in the UI.

## Shutdown and deployment

Keep the state directory private, back it up using the host policy, and do not
expose the stdio process as a network service. On graceful shutdown stop or
detach the worker, then call `recover` after restart to classify unfinished
runs. Run the deterministic suite, repository guard, wheel install smoke test
and the disposable provider acceptance job before enabling a provider in a
production deployment.
