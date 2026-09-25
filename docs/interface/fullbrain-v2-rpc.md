# Fullbrain v2 JSON-RPC reference

Start the installed `agentbridge --root PRIVATE_STATE_DIRECTORY rpc` process
inside a Fullbrain worker. Send one JSON-RPC 2.0 object per input line; read
one response line per request. Replace IDs, account references, model IDs and
paths below with values from that worker. These examples show the implemented
v2 request shape. Login and turn requests require a real provider and are not
live acceptance evidence merely because they are shown here. See the
[migration guide](fullbrain-v2-migration.md) for rollout blockers.

## Discover and create an account

```json
{"jsonrpc":"2.0","id":1,"method":"capabilities.get","params":{}}
{"jsonrpc":"2.0","id":2,"method":"accounts.list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"models.list","params":{"refresh":true}}
```

The first result has `contract_version: "v2"`, `execution_engine: "codex"`,
`route: "local_cliproxyapi"`, `providers`, `operations`, `parameters` and
`runtime_provider_support_verified: false`. Reject another contract version.
`accounts.list` returns a bare array of safe account projections. On a fresh
state root it returns `[]`. `models.list` returns an object with `source:
"agentbridge_routing"`, `stale`, `models`, `items`, `next_cursor` and
`has_more`. On a fresh root both model arrays are empty and the cursor is
`null`.

```json
{"jsonrpc":"2.0","id":4,"method":"accounts.login.start","params":{"provider":"codex","name":"team-codex-1","request_key":"login-1"}}
{"jsonrpc":"2.0","id":5,"method":"accounts.login.status","params":{"attempt_id":"SAVED_ATTEMPT_ID","owner_ref":"SAVED_OWNER_REF"}}
{"jsonrpc":"2.0","id":6,"method":"accounts.login.check","params":{"attempt_id":"SAVED_ATTEMPT_ID","owner_ref":"SAVED_OWNER_REF"}}
{"jsonrpc":"2.0","id":7,"method":"accounts.login.complete","params":{"attempt_id":"SAVED_ATTEMPT_ID","owner_ref":"SAVED_OWNER_REF"}}
```

For a Codex or Claude browser outside the worker host, the provider may end
at a localhost URL the browser cannot open. Fullbrain can accept that URL
once over HTTPS and relay it only in memory:

```json
{"jsonrpc":"2.0","id":16,"method":"accounts.login.callback","params":{"attempt_id":"SAVED_ATTEMPT_ID","owner_ref":"SAVED_OWNER_REF","redirect_url":"http://localhost:1455/auth/callback?code=ONE_USE_CODE&state=EXPECTED_STATE"}}
```

The callback uses the same attempt and verifies owner, provider and state.
Its response means only that the URL was delivered; Fullbrain still polls
`status`, then calls `check` and `complete`. The URL and code must stay out of
SQL, receipts, logs, browser storage and error text. Grok uses a device code.
This path has fixture tests, not live provider acceptance.

The start result includes `attempt_id`, `owner_ref`, `account_ref`,
`provider` and `status`, and may include `authorization_url`, `user_code`,
timestamps, safe `identity`, `verification` or `error`. Persist the two opaque
ownership references before showing the browser challenge. Status can be
`starting`, `awaiting_user`, `exchanging`, `authorized`, `verified`, `bound`,
`failed`, `cancelled`, `expired`, `interrupted`, `abandoned`, `revoked` or
`replaced`; handle unknown future states conservatively. Call `check` only
after authorization; call `complete` only after verification. Completion
returns `account` (safe public projection), `attempt` and `identity`. Cancel
with `accounts.login.cancel` and the same ownership references when the user
explicitly abandons a pending attempt. Interrupted OAuth can have an unknown
remote outcome; do not start a second attempt automatically.

Provider is the upstream account type: `codex`, `claude` or `grok`. Normal
onboarding lets AgentBridge manage one private CLIProxyAPI sidecar. Advanced
external routes supply all of `proxy_base_url`, `key_env` and
`management_key_env` to the **same** login flow. The last two are environment
variable *names*, never secret values. The browser mode remains `same_host`;
the explicit callback method relays a remote Codex or Claude redirect into
that same pending login.

## Read the model and account controls

```json
{"jsonrpc":"2.0","id":8,"method":"accounts.status","params":{"account_ref":"team-codex-1","refresh":true}}
{"jsonrpc":"2.0","id":9,"method":"accounts.usage","params":{"account_ref":"team-codex-1","refresh":true}}
{"jsonrpc":"2.0","id":10,"method":"models.list","params":{"refresh":true,"limit":100,"cursor":0}}
```

`accounts.status` reports configured provider/model references, observed
authentication and identity, and `routing.paused` with `routing.paused_at`.
The routing state is independent of authentication. `accounts.usage` reports `scope: "account"`,
`supported`, `stale` and `reason`; `source` and `quota_windows` are present
when known.
Each window has `id`, `label`, `scope`, optional `model_id`, `used_percent`,
`remaining_percent`, optional `window_seconds` and `resets_at`, `observed_at`,
`stale_at` and `stale`. Render each period separately, including Claude weekly
and scoped windows; an unknown value is not zero. `refresh: true` on usage
requests an upstream quota read through the bound local proxy for Codex or
Claude. A failed refresh leaves the last observation and its original age
intact and reports a safe `refresh_reason`. Use `stale_at` to expire a visible
percentage locally while the page stays open; request another explicit refresh
to obtain a new provider observation.
Usage history is available through `accounts.usage_history` or
`usage.history`; both return bare arrays, and historical time filters and
aggregation are currently unsupported.

Earned Codex resets use a separate, explicit account operation. Fullbrain
first calls `accounts.reset_credits` with `refresh: true` and displays the
returned `available_count`, optional credit details, `stale` and
`observation_ref`. It must not infer a credit from a window's `resets_at` or
from an additional pool such as `gpt-reserve`. After a user explicitly chooses
to redeem, Fullbrain saves a key for that one logical attempt and calls:

```json
{"jsonrpc":"2.0","id":18,"method":"accounts.reset_credits","params":{"account_ref":"team-codex-1","refresh":true}}
{"jsonrpc":"2.0","id":19,"method":"accounts.quota.reset","params":{"account_ref":"team-codex-1","idempotency_key":"SAVED_LOGICAL_ATTEMPT_KEY","observation_ref":"SAVED_FRESH_OBSERVATION_REF"}}
```

The credit observation must be fresh and available. If the redemption result
is unknown, keep its key and original parameters for explicit reconciliation;
do not create another key or switch accounts. The current adapter has fixture
evidence only; live provider acceptance remains pending. The complete result
and expiry rules are in [account-resets.md](account-resets.md).

Each `models.list.items[]` model has `id`, `display_name`, `availability`,
`source`, `providers`, `candidate_account_refs`, `observed_account_refs`,
`reasoning_efforts`, `context_windows`, `input_modalities` and
`account_capabilities`. Each account capability reports its own provider,
observation flag, reasoning efforts, default effort, context windows, input
modalities and metadata source. Treat empty metadata arrays as unknown
controls, not proof that a live model lacks those controls.
`configured_unverified` and `proxy_observed` do not prove that a live turn will
accept the model. The aggregate selector uses controls compatible across
observed accounts; Fullbrain can inspect account capabilities for a pin.
Pass `provider` to `models.list` to filter accounts before grouping and
pagination. A page therefore contains only models served by that provider;
`next_cursor` advances through the filtered catalog.

## Create and observe a conversation

```json
{"jsonrpc":"2.0","id":11,"method":"instances.create","params":{"model":"EXACT_MODEL_ID","workspace_path":"/existing/workspace","idempotency_key":"chat-1"}}
{"jsonrpc":"2.0","id":12,"method":"messages.create","params":{"instance_id":"SAVED_INSTANCE_ID","content":"Hello","idempotency_key":"message-1"}}
{"jsonrpc":"2.0","id":13,"method":"instances.events","params":{"instance_id":"SAVED_INSTANCE_ID","after_seq":0,"limit":100}}
{"jsonrpc":"2.0","id":14,"method":"turns.get","params":{"turn_id":"SAVED_TURN_ID","include_usage":true,"include_error":true}}
```

For a pinned route, add `"account_ref":"team-codex-1"` to
`instances.create`. For automatic routing within one provider, add
`"provider":"codex"`. Explicit `"routing_mode":"automatic"` may combine
an initial `account_ref` with a matching provider filter. Creation returns
`instance_id`, `routing_mode`, `routing_provider`, `affinity_account_ref`,
`account_ref`, `model` and other instance fields. Automatic routing keeps its
preferred account until confirmed exhaustion or ineligibility.
`messages.create` returns `turn_id`, `message_id`, `instance_id`, `state`,
`replayed` and the account actually selected for that turn in `account_ref`.
For an automatic route, `instances.create` and `messages.create` also accept
`excluded_account_refs`, a list of up to 1000 distinct account references.
Use the `account_ref` returned by `accounts.list` or `models.list`; accounts
with the same name across providers have distinct `id:<account_id>` refs.
An unqualified ambiguous name is rejected.
The router omits those accounts when choosing the initial route and the turn
route. A pinned route cannot use exclusions. Fullbrain pins the list in each
turn snapshot so retries and recovery use the same fence while an account
deletion is pending; it does not silently switch a pinned route.

`instances.events` and `turns.events` return bare arrays of events with `seq`,
`turn_id`, `instance_id`, `message_id`, `engine`, `kind`, `at`, `data` and
`final`. The event `engine` is `codex` for new v2 turns; provider is route
evidence. Persist the largest committed `seq` and request the next page with
`after_seq`. The `route.selected` data can include `account_changed`,
`portable_context_used` and `context_omitted_count`. A normal account or model
change retains the same native Codex session; it does not use portable context.
Keep the same `instance_id` for that chat across `instances.update` calls.
`final: true` on
`message.completed` does not finish a turn; wait for `run.finished` or check
`turns.get.state`. `instances.events` does not support `follow: true`.

For an isolated, disposable evaluation, create a fresh instance with
`"evaluation":true` and send exactly one message with a context package using
`"execution_mode":"evaluation_inputs_only"`. Persist the evaluation result
before calling `instances.discard_evaluation`:

```json
{"jsonrpc":"2.0","id":17,"method":"instances.discard_evaluation","params":{"instance_id":"SAVED_EVALUATION_INSTANCE_ID"}}
```

The result is `{ "instance_id": "...", "discarded": true, "pending": false }`
when cleanup is complete. If `pending` is true, poll the same request after
the turn and local processes finish. Repeating a completed discard returns
the same receipt; the old creation idempotency key cannot start the evaluation
again. Ordinary chat instances are never eligible for this operation. These
semantics have deterministic fixture coverage; live provider acceptance of the
inputs-only mode is separate.

Per-turn `context_window` is a positive numeric token count. Enable that UI
control only when the observed model metadata provides a maximum, and accept
that the provider may still reject it. Set per-turn `effort` only to a value
reported for the selected model and route. Per-turn `permission_mode` supports
`dontAsk` and `default`; with `default`, process `permission.required` and
respond through `permissions.respond` for the exact request. There is no
implicit approval for selected tools. `messages.create` accepts bounded
`context_package` and private Unix-socket `mcp` parameters. Follow the
[context and MCP contract](context-and-mcp.md); its deterministic fixture
coverage does not establish live provider or host-sandbox acceptance.

## Errors and uncertain work

Read `error.data.code`, `category`, `phase`, `outcome` and `retryable` from a
JSON-RPC error response. A numeric JSON-RPC code or message text is not a
stable product decision. `outcome: "unknown"` requires inspection and an
explicit user or host recovery decision; it never authorizes a new account or
an automatic rerun. `turns.stop` explicitly cancels a known turn; `recover`
classifies a lost worker. See [errors.md](errors.md) and
[events.md](events.md) for their limits.

## Pause or resume an account route

```json
{"jsonrpc":"2.0","id":16,"method":"accounts.pause","params":{"account_ref":"team-codex-1"}}
{"jsonrpc":"2.0","id":17,"method":"accounts.resume","params":{"account_ref":"team-codex-1"}}
```

Pause excludes the account from new automatic selections and pinned turns.
It preserves its authorization, history, quota observations and turns already
admitted. Resume restores eligibility after normal proxy checks. These calls
are idempotent; read `accounts.status.routing` to reconcile an uncertain
response. Paused accounts are absent from the routable `models.list` catalogue.

## Remove a local account route

After all active turns and login attempts finish, Fullbrain can issue this
independent request:

```json
{"jsonrpc":"2.0","id":15,"method":"accounts.delete","params":{"account_ref":"team-codex-1"}}
```

The result contains `account_ref`, `removed: true` and
`upstream_credential_removed: false`. Historical conversations and evidence
remain readable. This retires AgentBridge's local route; it does not revoke
the upstream credential stored by CLIProxyAPI. `accounts.delete` requires
exactly one of `account_ref` or `account_id`. A reference that matches an
account name and a different account ID is ambiguous; use the exact
`account_id` for an explicit retry. AgentBridge fences new routes before
stopping the sidecar. If stop confirmation is unknown, the call reports
`unknown_outcome` and `accounts.status` exposes
`retirement.local_proxy_stopped: false` without contacting the proxy. A
confirmed stop can be retried safely after a lost response.
