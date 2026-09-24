# Usage and execution failures

AgentBridge v2 runs Codex through a dedicated local CLIProxyAPI sidecar. The
Python SDK, CLI and JSON-RPC stdio API expose the same observations. Account
quota comes from the selected sidecar when its Management API supplies fresh,
attributable data. A local catalogue or OAuth file does not prove live model
entitlement or available quota.

## Queries

```sh
agentbridge accounts usage "My Codex" --refresh
agentbridge accounts history "My Codex" --json
agentbridge models list --refresh
agentbridge usage --turn-id TURN_ID --json
agentbridge usage --instance-id INSTANCE_ID --json
```

Equivalent RPC methods include `accounts.usage`, `accounts.usage_history`,
`models.list`, `usage.get` and `turns.get` with `include_usage` or
`include_error`. `turns.events` and `instances.events` expose bounded event
pages. Reading account usage or the local model catalogue does not start a
model turn.

## Proxy account quota

`accounts.usage` returns `account_id`, `scope: account`, `source`, `supported`,
`stale`, `quota_windows` and `reason`. Each sanitized window has an `id`,
`label`, `scope`, optional `model_id` or `model_family`, observed `used_percent`, derived
`remaining_percent`, optional `window_seconds` and `resets_at`, `observed_at`,
`age_seconds`, `stale_at` and `stale`. Periods and scoped pools stay separate;
the SDK does not collapse them to one account percentage. A provider may report
more than 100% utilization; the observed value is retained and remaining is
floored at zero. An unknown pool scope is never treated as model entitlement.
`source` is `cliproxy_management` for passive signals,
`cliproxy_upstream_usage` for an active response, or
`cliproxy_combined_usage` when distinct windows from both are retained. In a
combined response each window also names its source.

Passive observations read the dedicated CLIProxyAPI sidecar's bounded quota
signals. Codex primary, secondary and additional named windows and Claude
unified short, weekly and scoped windows are parsed from their reported fields,
without a fixed model list. `refresh=true` for `accounts.usage` or account
`usage.get` also asks Codex or Claude for current usage through the verified
sidecar credential. The SDK sends no credential value: CLIProxyAPI substitutes
the bound token. Only normalized quota facts are persisted; raw upstream bodies
and private errors are discarded. `accounts.status`, `models.list` and routing
observations do not trigger an upstream quota request. Grok quota remains
unknown unless a supported attributable source is added.

If a provider omits quota, the response has
`quota_windows: []`, `supported: false`, `stale: true` and a reason such as
`upstream_quota_unavailable`. A failed binding check also returns unknown
quota with a reason. If active refresh fails, the newest bound passive or prior
active observation remains visible with a safe `refresh_reason`; its original
time and per-window staleness remain intact. The displayed percentage is never
silently made current by a failed request. When windows have different ages,
the snapshot is stale only if no observed percentage is fresh; each window
retains its own `stale` and `stale_at`. A passed reset makes that old window
stale, not evidence of a renewed balance.

Automatic selection compares fresh model-applicable used percentages.
Accounts with known capacity take priority over unknown capacity; if none
can be compared, persisted turn counts break ties. A quota error or uncertain
turn does not trigger an automatic account change or retry. The selected
route remains fixed for the whole turn.

`accounts.history` reads stored observations. Each proxy row records its
source, scope, observed time, staleness and the sanitized quota snapshot.
History is evidence of what was seen then, not a fresh balance. Time-range
filters and aggregation are unsupported. An account usage refresh makes at most
one bounded upstream quota request for Codex or Claude after rechecking the
dedicated sidecar and its original GrantBridge binding. It does not infer usage
from an account label or start a model turn. Provider quota endpoints can be
unavailable or change independently of CLIProxyAPI; fixture tests establish
the request and normalization shape, while each provider still needs live
acceptance.

The local proxy does not expose Codex earned-reset redemption.
`accounts.quota.reset` returns unsupported in v2; there is no CLI
`accounts quota-reset` command. Historical native reset-credit operations do
not appear in current proxy account responses.

## Models and consumption

`models.list` aggregates exact IDs attached to proxy accounts and marks
configured candidates separately from accounts freshly observed in the
sidecar. Missing metadata is unknown. Membership is routing evidence, not
proof that an upstream provider will accept the model.

Turn token and cost observations retain their reported scope and source. A
Codex session total may be cumulative; it is not silently substituted for
one turn's usage. Instance usage returns at most 10,000 turn observations;
`partial` and `observation_limit` identify truncation. An absent turn usage
observation is unknown, not zero. Portable continuation records context
omissions separately from token usage.

## Failures and interruptions

`run.error`, terminal `run.finished` and `turns.get(include_error=true)` expose
safe structured failure data: code, category, phase, outcome, retryable and
action. Raw provider error bodies, credential values and private reasoning
are not persisted. Known structured codes take precedence over bounded
compatibility text; unrecognized evidence keeps an unknown classification.

| Code | Meaning |
| --- | --- |
| `safety_blocked` | Provider safety system rejected the request |
| `quota_exhausted` | Reported account allowance exhausted |
| `rate_limited` | Temporary provider rate limiting |
| `billing_required`, `budget_exhausted` | Billing or configured spend cap |
| `authentication_required`, `authorization_denied` | Login or access problem |
| `context_window_exceeded`, `output_limit_exceeded` | Context or output limit |
| `max_turns_exceeded`, `structured_output_failed` | Execution control stopped work |
| `provider_connection_lost`, `provider_timeout` | Transport ended or timed out |
| `unknown_outcome` | Completion or effects could not be established |

Provider retry notifications can be observed without starting another
AgentBridge turn. Partial text is marked incomplete. Retracted messages are
omitted from visible transcript and exported context while their evidence
events remain; retraction does not undo a tool effect. Explicit Stop remains
cancelled. A clean process exit without a completion event is not success.
In-flight tools without results keep unknown outcomes. Terminal failures are
not automatically retried, and a safety block never triggers fallback to
another account.

Unknown future failures may enter the
[reviewed error-learning workflow](error-learning.md), with explicit rule
activation. See [implementation status](interface/implementation-status.md)
for the proxy path's acceptance limits.
