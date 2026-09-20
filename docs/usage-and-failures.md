# Usage, resets and execution failures

This implemented local contract is shared by the Python SDK, CLI and JSON-RPC
stdio dispatcher. It does not require a web server or a Fullbrain installation.

## Queries

```sh
agentbridge accounts usage "My Codex" --refresh
agentbridge accounts usage "My Claude" --refresh --json
agentbridge accounts history "My Claude" --json
agentbridge models list --engine cursor --account-ref "My Cursor" --refresh
agentbridge usage --turn-id TURN_ID --json
agentbridge usage --instance-id INSTANCE_ID --json
```

Equivalent RPC methods are `accounts.usage`, `accounts.usage_history`,
`models.list`, `usage.get`, `turns.get` with `include_usage`/`include_error`,
and bounded `turns.events` or `instances.events`.

Live queries need an explicit bound account. Reading a catalog or quota does
not start a model turn. Without refresh, models use a clearly marked static
fallback; account usage reads cached observations or available Codex rollouts.

## Quota windows

Account responses add `schema_version: 1`, `windows`, `age_seconds` and `stale`.
Each window has an opaque stable `id`, `pool_id`, `name`, `label`, `scope`,
`model_id`, `model_family`, `window_seconds`, `used_percent`,
`remaining_percent`, `resets_at`, `reset_after_seconds`, `reset_due` and
`limit_reached`. Scoped Claude limits also retain safe model/surface labels,
surface ID, provider kind and group. A label is not a model ID.

- Codex reads every reported `rateLimitsByLimitId` bucket, including its
  primary and secondary windows. The legacy single bucket is deduplicated.
  Duration comes from the provider, never an assumed five-hour or weekly plan.
- Claude reads five-hour, weekly and model-family windows, as well as dynamic
  `limits[]` scoped by model, surface and group. Unknown future pool names
  remain observable with unknown scope. It does not guess what models an
  opaque pool covers or assume switching model will bypass a shared cap.
- Claude stream events express utilization as a fraction, whereas the native
  OAuth quota response expresses percentages. The adapter normalizes both
  explicitly, including sparse events and `unifiedWindows`.
- Countdown is recomputed when reading. A reset time in the past marks the
  snapshot stale; it does not replace consumption with zero. Missing numbers,
  reset times or capacities remain null. Percentages over 100 are retained,
  with remaining percent floored at zero.
- Provider snapshots become stale after 60 seconds; rollout observations after
  30 minutes. Failed refreshes invalidate the previous cached snapshot.
  Historical rows preserve the time of the observation.
- Multiple windows can constrain the same work. Do not add their percentages
  or treat them as independent budgets.

Codex `pools` additionally exposes plan, provider credit balance, individual
spend limit and `spend_control_reached`, even when there are no time windows.
Amounts without a declared currency preserve their decimal representation
with `unit: null`. Claude `extra_usage` preserves reported spend/limit,
utilization and currency without converting provider units.

The Cursor SDK does not expose remaining account quota or its reset schedule.
Account usage returns `supported: false` with
`sdk_account_quota_unavailable`. Session token/cost observations, API request
rate limits and team billing APIs are not substitutes for account quota.

## Explicit Codex earned resets

Quota responses expose `reset_credits.available_count` and optional details.
Null count means unknown. Null details means only a count was reported; an
empty array means the provider returned no detail rows. Never derive the
available count from a possibly truncated detail list.

```sh
agentbridge accounts quota-reset "My Codex" \
  --idempotency-key reset-request-001 --credit-id PROVIDER_CREDIT_ID --json
```

RPC: `accounts.quota.reset` with `account_ref`, `idempotency_key` and optional
`credit_id`. This is an explicit account mutation. The embedding host must
obtain its normal authorization before calling it; usage queries never redeem
credits. AgentBridge does not implement business approval policies.

The intent and provider principal are persisted before submission. Concurrent
requests are excluded per account. A completed request replays its durable
receipt after restart; a conflicting account/credit is rejected. A lost
response returns `unknown_outcome`, not an automatic retry or a new reset key.
Explicit reconciliation uses the original key. A second logical reset is
blocked while the first is unresolved.

Known outcomes are `reset`, `already_redeemed`, `nothing_to_reset` and
`no_credit`. Read fresh quota after a known result. This operation has isolated
provider-fixture acceptance; no earned credit was consumed during validation.

## Models and consumption

Catalogs preserve reported effort values/options/defaults, context windows,
maximum output tokens, modalities, service tiers and capability flags. Claude
resolved model IDs and Cursor parameters/variants are retained. Missing
metadata is unknown, including context size when only a name contains `[1m]`.
Membership is not proof of account entitlement. Codex model discovery consumes
bounded native pages with repeated-cursor detection.

Cursor effort is encoded using a model parameter only after the account's
catalog advertises both that parameter and the requested value. Missing or
invalid values fail before creating a provider agent, without switching models.

Token and cost observations retain scope. Codex session totals are cumulative;
its latest turn observation is separate. Claude main-loop tokens are per turn,
while `modelUsage` and total cost may include resumed-session history and are
reported as cumulative session observations. They are not summed across turns.
Model-specific context/output sizes reported during execution are observations,
not universal catalog entitlements or a context-selection control.

## Failures and interruptions

`run.error`, terminal `run.finished` and `turns.get(include_error=true)` expose
safe structured failure data: code, category, phase, outcome, retryable and
action. Original provider bodies, safety explanations and credentials are not
persisted. Classification prefers native codes, with bounded text matching for
known compatibility messages and an explicit detection source.

| Code | Meaning |
| --- | --- |
| safety_blocked | Provider safety system rejected the request |
| quota_exhausted | Reported account usage allowance exhausted |
| rate_limited | Temporary provider rate limiting |
| billing_required / budget_exhausted | Billing or configured spend cap |
| authentication_required / authorization_denied | Login or access problem |
| context_window_exceeded / output_limit_exceeded | Context or output limit |
| max_turns_exceeded / structured_output_failed | Execution control stopped work |
| provider_connection_lost / provider_timeout | Transport ended or timed out |
| unknown_outcome | Completion or effects could not be established |

Provider-native retry notifications remain observable without starting another
AgentBridge turn. Native model fallback/rerouting is observable as model changes.
Retracted provider messages are omitted from the visible transcript and exported
context; their original events remain in the evidence archive. Partial text is
marked incomplete. Retractions do not prove that earlier tool effects vanished.
Explicit Stop remains cancelled. A clean process exit without a completion
event is not success. In-flight tools without results keep unknown outcomes.
Terminal execution failures are not automatically retried, and a safety block
never triggers an AgentBridge fallback to evade the block.

Unknown future failures still return a safe generic code; the adapter does not
claim exhaustive knowledge of future provider messages. See the remaining
scope in [implementation status](interface/implementation-status.md).

## Protocol evidence

- Installed Codex 0.153.0 generated app-server JSON schema and
  [official app-server reference](https://learn.chatgpt.com/docs/app-server).
- Claude Code 2.1.266 native schema, official Agent SDK 0.3.278 declarations,
  [SDK reference](https://code.claude.com/docs/en/agent-sdk/python) and
  [usage and costs](https://code.claude.com/docs/en/costs).
- Cursor SDK 1.0.31 types and
  [official Python SDK reference](https://prod.cursor.com/docs/sdk/python).

Claude OAuth quota and unified stream windows are versioned native
compatibility surfaces, not a promised stable public subscription API.
