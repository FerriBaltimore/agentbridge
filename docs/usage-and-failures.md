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
`stale`, `quota_windows` and `reason`. A reported window contains only
`model_id`, `used_percent` and `observed_at`. These are sanitized
CLIProxyAPI Management API observations, not the richer native Codex or
Claude quota structures from the earlier direct adapters.

The current observer accepts attributable percentage quota for a Codex
upstream account. Claude and Grok account quota is unavailable through this
local observer. If a provider omits quota, the response has
`quota_windows: []`, `supported: false`, `stale: true` and a reason such as
`upstream_quota_unavailable`. A failed binding check also returns unknown
quota with a reason. A reported value whose observation is too old is stale;
AgentBridge does not turn it into zero or claim renewed capacity.

Automatic selection compares fresh model-applicable used percentages.
Accounts with known capacity take priority over unknown capacity; if none
can be compared, persisted turn counts break ties. A quota error or uncertain
turn does not trigger an automatic account change or retry. The selected
route remains fixed for the whole turn.

`accounts.history` reads stored observations. Each proxy row records its
source, scope, observed time, staleness and the sanitized quota snapshot.
History is evidence of what was seen then, not a fresh balance. Time-range
filters and aggregation are unsupported. An account usage refresh checks the
local sidecar; it does not query a provider dashboard or infer usage from an
account label.

The local proxy does not expose Codex earned-reset redemption.
`accounts.quota.reset` returns unsupported in v2; there is no CLI
`accounts quota-reset` command. Historical native reset-credit and Claude
OAuth quota formats do not appear in current proxy account responses.

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
