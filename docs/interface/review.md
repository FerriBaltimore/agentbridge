# Interface completeness review

This checklist is the second pass over the provider abstraction. It prevents a
feature from existing only as a happy-path method.

## Design checklist, not an implementation claim

The following list records the intended scope. Actual coverage and remaining
work are in [implementation-status.md](implementation-status.md).

- Provider discovery: proxy provider, versions, capabilities and parameters.
- Accounts: one asynchronous proxy login, status, identity and cancellation.
- Models: live catalog, cache, static fallback, deprecation and retirement.
- Instances: create, inspect, list, update, archive, delete and transfer.
- Messages: text, multimodal blocks, attachments, transcript pagination and
  idempotent admission.
- Turns: asynchronous start, status, event following, stop, resume and recovery.
- Permissions: pending request, explicit host response, expiry and denial.
- Usage: account, instance and turn scopes, quota windows, history and staleness.
- Errors: stable codes, action hints, retryability and unknown outcomes.
- Evidence: event sequence, provider source, adapter version and observation time.

## Required adapter behavior

The Codex execution adapter and each upstream proxy provider must define:

- Supported operations and parameters.
- Validation before admission.
- Native identifiers and version compatibility.
- Stop behavior and terminal-state mapping.
- Usage and quota source, scope and staleness.
- Unknown provider fields as gap events.
- Secret and private-reasoning redaction.
- Immutable native Codex session binding across model and upstream route changes.
- Explicit portable export or transfer into a separate instance.

## Required tests

Each operation needs a deterministic fake-provider test. Each provider-specific
claim needs a separate provider acceptance record. Tests must cover:

- invalid parameters and unsupported capabilities;
- duplicate idempotency keys and concurrent turns;
- stream gaps, malformed events and provider disconnects;
- stop before launch, during execution and after completion;
- authentication, quota, timeout and permission errors;
- lost workers and unknown side effects;
- model, provider and account changes preserving native session identity;
- missing or divergent native state failing without a replacement thread;
- stale usage and fallback model catalogs;
- transcript and event pagination;
- installed wheel CLI and JSON-RPC behavior.

## Explicit non-goals

AgentBridge does not own OAuth credentials, host authorization policy, business
retries, external side effects or private model reasoning. GrantBridge
coordinates browser authorization; CLIProxyAPI stores and renews the upstream
credential. AgentBridge selects an account only before an automatic turn.
