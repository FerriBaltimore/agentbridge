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

`accounts.list` returns safe public items with `account_ref`, `name`, `email`
and, when present, `provider`, `supported_models`, `authentication`,
`identity`, `routing` and `reason`. `routing` has `paused` and `paused_at`;
it describes local admission, separately from observed authentication.
`accounts.status` adds a configured reference summary
and observations; it is not a credential export.

Authentication is an observation, not a promise that a future model request
will succeed. Internal account configuration references `proxy_base_url`,
`key_env` and `management_key_env`; these are omitted from the public RPC
projection. The supervisor generates managed keys and passes them to local
processes when required; values are never stored in the AgentBridge database.
The management key is required for local login,
identity, route and quota checks on both automatic and pinned accounts. It is
never passed to Codex. Historical `engine` and home fields may appear only in
old stored records; they do not enable direct execution.

## Model

An implemented `models.list` item has:

    id, display_name, availability, source, providers,
    candidate_account_refs, observed_account_refs, reasoning_efforts,
    context_windows, default_context_window, max_context_window,
    input_modalities, account_capabilities

Each `account_capabilities` item has `account_ref`, `provider`, `observed`,
`reasoning_efforts`, `default_reasoning_effort`, `context_windows`,
`default_context_window`, `max_context_window`, `input_modalities` and
`metadata_source`. The aggregate result also has
`source`, `stale`, `models`, `items`, `next_cursor` and `has_more`.
`candidate_account_refs` are declarations; `observed_account_refs` have fresh
local proxy evidence. The item's `source` is `account_configuration` and its
`availability` is `configured_unverified` or `proxy_observed`. Neither
availability proves live entitlement. Missing metadata is an empty list or
null source, never an invented default. Generic target fields such as
`description`, `is_default`, retirement, tool support and service tier are not
returned by the current v2 model item.

`context_windows` contains selectable token counts observed in the local
Codex client catalog: its default context window and, when larger, its
reported maximum. These are suggestions derived from observed bounds, not an
exhaustive provider enum. `default_context_window` is the observed default or
null. `max_context_window` is the reported maximum, or the observed default
as the only known safe ceiling when no maximum is reported. A conflicting
default above the reported maximum is omitted. In an automatic model item,
the maximum is the smallest known ceiling across observed eligible accounts;
its choices are observed values within that ceiling. If any such account has
no known ceiling, the aggregate has no context choices or maximum. Account
filtering recomputes these fields. `max_output_tokens`, where separately
available in native metadata, describes output capacity and is not a context
window or a supported v2 output-budget control. Context window values are
nominal model settings; Codex may reserve part of that window for instructions,
tools and output, so they are not promised user-prompt token budgets.

## Instance

An instance is a durable conversation. An automatic instance may move to
another proxy account between turns. The selected account remains fixed during
one turn, and the actual account is recorded with that turn. A pinned instance
stays on its selected account. Every instance binds once to one native Codex
session, independently of its upstream account, provider or model. Historical
direct instances are read only.

    instance_id, account_ref, routing_mode, workspace_path, model, effort,
    context_window, permission_mode, sandbox_mode, allowed_tools,
    native_session_id, state, adapter_version, provider_version,
    created_at, updated_at, metadata

`routing_mode` is `automatic` or `pinned`. On an automatic instance,
`account_ref` identifies the selected or admitted route, including after a
failed turn, or its initial candidate before the first turn. `native_session_id`
can be absent before Codex creates the initial thread; once bound, it is
immutable. Switching model, account or provider retains that thread and its
native history. Missing or divergent
native state is an explicit continuation failure. Bounded portable export
belongs to an explicit transfer into a separate instance, not a route change.

For existing records, migration anchors the stored native session or the
latest observed historical session ID when the stored binding is absent.
Admission compares that binding with the latest observed session identity.
If an older implementation restored thread A after a later thread B failed,
the differing observation returns `native_session_diverged`; it cannot
silently resume A and omit B. Continuing requires explicit review and recovery
or an explicit transfer into a separate conversation. Migration does not
select a different thread to resolve the ambiguity or merge native histories.

## Message and turn

    message_id, instance_id, role, content, attachments, sequence, created_at
    turn_id, message_id, account_ref, state, attempt, started_at, finished_at,
    usage, error, outcome, idempotency_key

messages.create adds conversation input. turns.resume recovers a known turn.
They are intentionally different operations.
Queued input has its own durable `message_id` before a `turn_id` or account is
assigned. A steering message shares the active `turn_id` while retaining its
own message identity. Read [message-queues.md](message-queues.md) for queue
records, positions, states and delivery controls.
The accepted message and `turns.get` identify the actual account selected for
their run. Route events carry safe selection evidence: account ID, model,
quota state, selection reason and count of omitted context items. They contain
no proxy endpoint or credential reference.

## Usage and quota

Usage has scope account, instance or turn and includes source, timestamp,
staleness and support state. Quota windows include used values, limits and reset
times when the provider exposes them. Absence is not zero.
Account windows are separate observations with stable IDs, labels, periods,
reset times, scope, used and remaining percentages, and individual freshness.
Codex and Claude usage refreshes query the provider through the verified local
CLIProxyAPI account; status and model reads remain passive. Raw provider
responses and credential values are not stored. A failed refresh retains
the last observed values with their original age and a safe failure code.
