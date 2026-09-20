# Interface resources

## Account

An account binds an engine to credential references and, for native providers,
an isolated home. It stores no secret value.

    account_ref, engine, name, email, home_ref, credential_refs,
    authentication, identity, observed_at

authentication is an observation. It is not a promise that a future model
request will succeed. `home_ref` and `credential_refs` are internal storage
references and are omitted from the public RPC projection.

## Model

    id, display_name, description, is_default, availability,
    deprecated, retirement, reasoning_efforts, context_windows,
    input_modalities, tool_support, subagent_support, service_tiers,
    source, observed_at, stale, provider_extensions

source is live, cache or static. stale is explicit. Missing model metadata is
unknown, never an invented default.

## Instance

An instance is the durable conversation binding:

    instance_id, engine, account_ref, workspace_path, model, effort,
    context_window, permission_mode, sandbox_mode, allowed_tools,
    native_session_id, state, adapter_version, provider_version,
    created_at, updated_at, metadata

The native session ID is optional and must not be required for portable
continuity.

## Message and turn

    message_id, instance_id, role, content, attachments, sequence, created_at
    turn_id, message_id, state, attempt, started_at, finished_at,
    usage, error, outcome, idempotency_key

messages.create adds conversation input. turns.resume recovers a known turn.
They are intentionally different operations.

## Usage and quota

Usage has scope account, instance or turn and includes source, timestamp,
staleness and support state. Quota windows include used values, limits and reset
times when the provider exposes them. Absence is not zero.
