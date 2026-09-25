# Error contract

JSON-RPC keeps its standard numeric code, while error.data.code carries the
stable AgentBridge code:

    code: -32000
    message: Authentication is required for this account.
    data:
      code: authentication_required
      category: auth
      retryable: false
      action: login
      phase: admission
      outcome: not_started
      retry_after_ms: null
      details: {}

## Stable codes

    invalid_request, invalid_engine, unsupported_operation,
    unsupported_parameter, account_not_found, account_busy,
    authentication_required, authorization_denied, instance_not_found,
    instance_busy, turn_not_found, idempotency_conflict, model_not_found,
    model_unavailable, quota_exhausted, provider_timeout,
    provider_unavailable, provider_protocol_error, provider_failed,
    permission_required, permission_denied, continuity_unavailable,
    native_session_missing, native_session_diverged,
    native_version_unverified, cancelled,
    interrupted, unknown_outcome, store_unavailable

Authentication orchestration also uses `authentication_attempt_not_found`,
`authentication_owner_required`, `authentication_attempt_not_ready`,
`authentication_not_verified`, `authentication_in_progress`,
`authentication_outcome_unknown`, `activation_unsupported` and
`login_timeout`. A browser OAuth start may return
`oauth_callback_port_busy` or `oauth_callback_unavailable` before dispatch
when the local callback listener cannot be opened. Proxy onboarding and
routing additionally use
`account_migration_required`, `proxy_binding_unverified`,
`proxy_binding_changed`, `proxy_endpoint_shared`, `model_required`,
`context_window_unavailable` and `context_stale`. `context_window_unavailable`
rejects a turn before execution if no verified eligible account has a known
ceiling large enough for its requested context override. Account retirement may return
`busy` when a turn is active; `account_unavailable` identifies an incompatible
historical account record.
Inputs and observations also use `invalid_attachment`, `invalid_permissions`,
`invalid_execution_policy` (incompatible access and selected-input isolation),
`permission_expired`, `provider_catalog_unsupported` and `rate_limited`.
Queues additionally use `queue_full`, `invalid_position`, `message_not_found`,
`message_not_pending`, `message_dispatching`, `interrupt_pending`,
`queue_dispatcher_unavailable`, `queue_dispatch_failed`, `invalid_delivery`,
`turn_conflict`, `turn_not_active`, `steering_unsupported`, `steering_rejected`,
`steering_options_conflict`, `steering_context_unsupported` and
`evaluation_queue_unsupported`. Lost private bindings use `context_required`;
a lost live-input acknowledgement is `unknown_outcome`. Queue errors after a
durable admission include `details.message_id` so callers can inspect or remove
that exact saved input instead of submitting a duplicate.
Execution adapters additionally distinguish `safety_blocked`, `billing_required`,
`budget_exhausted`, `context_window_exceeded`, `output_limit_exceeded`,
`max_turns_exceeded`, `structured_output_failed` and `provider_connection_lost`.
Reset redemption uses `reset_pending`, `account_changed`, `identity_missing`
and `identity_changed`; unknown redemption outcomes retain the original key.
See [usage and failures](../usage-and-failures.md) for scope and validation.
Reviewed learning additionally uses `invalid_error_evidence`,
`invalid_error_proposal`, `error_record_not_found`, `error_proposal_not_ready`,
`error_validation_failed`, `error_rule_conflict`, `version_conflict`,
`diagnosis_failed`, `diagnosis_invalid` and `diagnosis_timeout`. A completed
diagnostic proposal is not an active rule. See [error learning](../error-learning.md).
They use the same envelope and never expose provider credentials or native home
paths in RPC responses.

action is login, wait, change_account, change_model, resume, inspect or none.
retryable is a provider-safe recommendation, not permission to rerun a side
effect.

The implemented envelope uses outcome=unknown for an uncertain result. It is
never retryable. not_started identifies rejection before work; other outcomes
are supplied by the operation. The code catalogue above also includes target
and historical compatibility codes. `invalid_engine` describes old records;
it does not enable a direct execution path. V2 continuation also uses
`native_session_missing` when the bound session is unavailable and
`native_session_diverged` when its identity is replaced or mismatched. Neither
error permits a replacement thread, portable reconstruction or automatic rerun.
The mismatch check includes a legacy stored identity that differs from the
latest observed native thread, even if the stored thread previously completed
successfully. Explicit review and recovery or a separate transfer is required.
Unknown provider stderr, tokens, prompts, private reasoning and unbounded
response bodies are never included in the public error.

Adapters map their errors once. AgentBridge owns automatic account selection
before each admitted turn. The host owns product retry policy and explicit
recovery or new-turn decisions after a failure; no layer silently reruns an
uncertain turn on another account.
