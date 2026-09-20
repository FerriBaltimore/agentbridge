# Fullbrain integration

Fullbrain should treat AgentBridge as a local execution kernel over JSON-RPC
stdio. It owns product policy, account selection, permissions, retries and UI.
AgentBridge owns provider processes, durable execution state and normalized
observations. Fullbrain must not call GrantBridge or a provider CLI directly.

## Startup

1. Start one AgentBridge process for the Fullbrain worker and give it a private
   state directory.
2. Call `capabilities.get` and retain the contract version and maturity fields.
3. Call `accounts.list`, show only its safe projection, and choose an explicit
   `account_ref`.
4. Call `instances.create` with that account and an idempotency key.

The selected account reference is stored with the Fullbrain conversation. An
instance is never created by implicit engine ordering or a silent fallback.

## Conversation loop

Call `messages.create` with an idempotency key. Persist both returned IDs. Poll
`instances.events(instance_id, after_seq, limit)` and advance the Fullbrain
cursor only after committing each page. Translate normalized events to the UI.
Use `turns.get` for a complete turn and `turns.events` when a single-turn view
is needed. Do not use `follow=true` in the first integration.

On reconnect, reopen the same instance and cursor. A timeout or disconnect is
not permission to submit the message again. Reuse the same idempotency key and
inspect the returned `replayed` flag. If a turn ends with `unknown_outcome`,
show that state and require an explicit recovery decision.

## Authentication

For onboarding, call `accounts.login.start`, persist `attempt_id` and
`owner_ref`, and display the returned authorization URL or user code. Poll
`accounts.login.status`, then call `accounts.login.check` and
`accounts.login.complete`. The complete operation performs its own fresh check,
so a successful browser callback alone never creates a usable account. After a
restart, resume with the persisted references. Cancel with `login.cancel`.

## Errors and capability gates

Branch on `error.data.code`, `category`, `phase`, `outcome` and `retryable`, not
on message text. Never automatically retry `unknown_outcome`. A capability is
usable only when `support` is not `unsupported` and its maturity meets the
deployment gate. Fixtures and controlled live acceptance are recorded in
provider-acceptance.md; the actual Fullbrain deployment must verify its selected
provider, version and supported feature subset separately.

## Shutdown and deployment

Keep the state directory private, back it up using the host policy, and do not
expose the stdio process as a network service. On graceful shutdown stop or
detach the worker, then call `recover` after restart to classify unfinished
runs. Run the deterministic suite, repository guard, wheel install smoke test
and the disposable provider acceptance job before enabling a provider in a
production deployment.
