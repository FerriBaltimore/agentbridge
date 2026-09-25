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
    native_session_missing, native_version_unverified, cancelled,
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
`permission_expired`, `provider_catalog_unsupported` and `rate_limited`.
Execution adapters additionally distinguish `safety_blocked`, `billing_required`,
`budget_exhausted`, `context_window_exceeded`, `output_limit_exceeded`,
`max_turns_exceeded`, `structured_output_failed` and `provider_connection_lost`.
V2 Codex reset redemption uses `reset_observation_changed`,
`reset_observation_stale`, `reset_credit_unavailable`,
`reset_credit_changed`, `reset_observation_used`, `reset_pending` and
`reset_in_progress`. `reset_outcome_unknown` has `outcome: unknown` and retains
the original key; `reset_attempt_not_started` requires a new observation.
See [earned resets](account-resets.md) for the exact recovery flow.
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
and historical compatibility codes. `invalid_engine` and native-session codes
describe old records; they do not enable a direct execution path.
Unknown provider stderr, tokens, prompts, private reasoning and unbounded
response bodies are never included in the public error.

Adapters map their errors once. AgentBridge owns automatic account selection
before each admitted turn. The host owns product retry policy and explicit
recovery or new-turn decisions after a failure; no layer silently reruns an
uncertain turn on another account.
