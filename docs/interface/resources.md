# Interface resources

## Account

An account has a provider (`codex`, `claude` or `grok`), exact observed model
IDs and a dedicated loopback proxy route. Codex is the sole execution engine.
`accounts.login.start(provider, name)` prepares an isolated local proxy by
default; advanced callers may supply an existing proxy route to the same
flow. `accounts.login.complete` creates the account after GrantBridge-
coordinated OAuth and fresh sidecar identity and model checks. CLIProxyAPI
owns the upstream OAuth credential and its auth directory. Historical direct
account records remain readable only. Configuration stores references, never
key values or upstream credentials.

    account_ref, provider, supported_models, name, email,
    proxy_base_url, key_env, management_key_env,
    authentication, identity, observed_at

Authentication is an observation, not a promise that a future model request
will succeed. The proxy URL and key environment references are omitted from
the public RPC projection. Managed key values exist only in supervisor memory,
not the AgentBridge database. The management key is required for local login,
identity, route and quota checks on both automatic and pinned accounts. It is
never passed to Codex. Historical `engine` and home fields may appear only in
old stored records; they do not enable direct execution.

## Model

    id, display_name, description, is_default, availability,
    deprecated, retirement, reasoning_efforts, context_windows,
    input_modalities, tool_support, subagent_support, service_tiers,
    source, observed_at, stale, provider_extensions

source is live, cache or static. stale is explicit. Missing model metadata is
unknown, never an invented default. A configured proxy model is a routing
declaration, not a live model catalog or entitlement observation. Aggregate v2
models add `providers`, `candidate_account_refs`, and `observed_account_refs`;
the latter requires fresh local proxy evidence. Its `availability` is
`configured_unverified` or `proxy_observed`, neither of which certifies live
provider acceptance.

## Instance

An instance is a durable conversation. An automatic instance may move to
another proxy account between turns. The selected account remains fixed during
one turn, and the actual account is recorded with that turn. A pinned instance
stays on its selected account. Historical direct instances are read only.

    instance_id, account_ref, routing_mode, workspace_path, model, effort,
    context_window, permission_mode, sandbox_mode, allowed_tools,
    native_session_id, state, adapter_version, provider_version,
    created_at, updated_at, metadata

`routing_mode` is `automatic` or `pinned`. On an automatic instance,
`account_ref` identifies the last completed route, or its initial candidate
before the first turn. The native session ID is optional and must not be
required for portable continuity. Switching accounts creates a new native
Codex thread from bounded portable context and reports omitted context.

## Message and turn

    message_id, instance_id, role, content, attachments, sequence, created_at
    turn_id, message_id, account_ref, state, attempt, started_at, finished_at,
    usage, error, outcome, idempotency_key

messages.create adds conversation input. turns.resume recovers a known turn.
They are intentionally different operations.
The accepted message and `turns.get` identify the actual account selected for
their run. Route events carry safe selection evidence: account ID, model,
quota state, selection reason and count of omitted context items. They contain
no proxy endpoint or credential reference.

## Usage and quota

Usage has scope account, instance or turn and includes source, timestamp,
staleness and support state. Quota windows include used values, limits and reset
times when the provider exposes them. Absence is not zero.
