# Account affinity and provider cache

## Routing choices

Automatic routing stores one preferred account per conversation. It preserves
that preference across process restarts and completed or failed turns.

| Situation | Behavior before a new turn |
| --- | --- |
| Initial account supplied with automatic mode | Save it as the initial affinity |
| No initial account | Select the eligible account with the lowest comparable usage |
| Another account has lower usage | Keep the current account |
| Current usage is missing or unknown | Keep the current eligible account; do not claim zero usage |
| Fresh applicable quota window is exhausted | Select another eligible account and save the new affinity |
| Old account regains quota | Keep the new account |
| Current account is busy or temporarily rate limited | Return a retryable error without changing affinity |
| Identity verification fails | Block execution without treating the failure as exhausted quota |
| Account paused, retired, excluded, or incompatible with an explicit model/provider change | Select an eligible replacement |
| Pinned mode | Use the selected account; no automatic replacement |

Selection uses every applicable quota window, including account-wide and
model-specific limits. The most constrained known window governs capacity.
An unproven scope or missing counter keeps the comparison unknown. Fresh
exhaustion is enough to reject a route even if another window is unknown.
Active Codex and Claude quota reads are bounded and reuse recent evidence;
Grok remains unknown unless the proxy supplies attributable quota evidence.
An expired rejection with a future reset cannot establish replenished quota:
it requires revalidation. A passed reset allows another attempt on the same
account with unknown usage. A generic 429 is not evidence of credit exhaustion.

Automatic failover occurs at admission of a new, explicitly submitted turn.
It never retries a failed request, replays a tool call or changes credentials
inside an executing turn. Stop remains explicit cancellation. Queue delivery
does not grant permission to replay a failed turn. A route decision, its
exhaustion evidence when available, and the new affinity are committed in the
same transaction before execution.

## SDK and CLI

```python
instance = bridge.instance_create(
    model="your-model-id", workspace_path=".",
    routing_mode="automatic", account_ref="Example")

bridge.instance_update(instance["id"], routing_mode="pinned",
                       expected_version=instance["version"])
```

```sh
agentbridge instances create --model your-model-id \
  --routing-mode automatic --account-ref Example
```

An account alone pins the instance. Automatic mode plus an account sets the
initial or next preferred account. Changing mode without an account retains
the current preference. The playground exposes both controls and the active
turn's account. The controls are disabled during an active turn. The server
also rejects route edits atomically when pending queue work occupies the
conversation; the playground retains the draft if that update is rejected.

`affinity_account_ref` is the preferred next route. `account_ref` identifies
the stored native-session owner or explicit manual selection. After a failed
handoff these may differ: retaining the last completed native session does
not revert affinity. Each turn reports its actual account separately.

## Cache preservation

Keeping the account, native thread, exact model and unchanged configuration
stable allows Codex to resume its existing thread and preserve the reusable
prompt prefix. AgentBridge does not add rotating cache keys, time stamps,
per-turn route banners or quota data to that prompt. It does not rebalance
because another account becomes cheaper or less used during a conversation.

A genuine account change cannot reuse the old account's native thread.
AgentBridge starts a new one with at most 128,000 bytes of portable evidence,
including omissions and unknown outcomes. This sacrifices cache continuity
to preserve correct account ownership and conversation context. A manual
A → B → A change without an intervening turn can restore A's last completed
native thread when its ownership and terminal state are established.

Native compaction, model changes and provider-side transformations can alter
the prefix. Cache lifetime, eviction, cache-write pricing, quota accounting
and sharing across credentials are provider and product specific. Public API
documentation does not establish the behavior of an OAuth subscription route.
No client-side affinity policy can guarantee a cache hit or an exact saving.
AgentBridge does not expose unsupported native cache retention, breakpoint
or provider-options controls.

## Observability and acceptance

Turn usage retains persisted native observations and their scope. Each has a
`cache` projection with `read_tokens_reported`, `write_tokens_reported`,
`source`, `scope` and `aggregation`. Missing or invalid counters remain `null`.
Native zero reports remain visible but `zero_is_conclusive` is false because
Codex or a translator can synthesize zero for an absent provider counter.
`upstream_verified` is false and monetary `cost` is unknown. Session totals
and latest observations overlap and are never added together.

Deterministic selector, SQLite, local subprocess and browser fixtures validate
affinity and continuity. See the [affinity lab](../development/account-affinity-lab.md).
They do not establish live provider cache acceptance or measured cost savings.
A future live acceptance run must record redacted request-prefix evidence,
provider-reported cache reads and writes, their scope, model/product identity,
and billed cost or consumed credits per completed task. Account quota and
cached-token rates alone cannot measure that saving.
