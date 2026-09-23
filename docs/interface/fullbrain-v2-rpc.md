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
variable *names*, never secret values. The currently accepted browser mode is
local `same_host`; the remote Fullbrain browser flow needs further work.

## Read the model and account controls

```json
{"jsonrpc":"2.0","id":8,"method":"accounts.status","params":{"account_ref":"team-codex-1","refresh":true}}
{"jsonrpc":"2.0","id":9,"method":"accounts.usage","params":{"account_ref":"team-codex-1","refresh":true}}
{"jsonrpc":"2.0","id":10,"method":"models.list","params":{"refresh":true,"limit":100,"cursor":0}}
```

`accounts.status` reports configured provider/model references and observed
authentication and identity. `accounts.usage` reports `scope: "account"`,
`supported`, `stale` and `reason`; `source` and `quota_windows` are present
when known.
Usage history is available through `accounts.usage_history` or
`usage.history`; both return bare arrays, and historical time filters and
aggregation are currently unsupported.

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

## Create and observe a conversation

```json
{"jsonrpc":"2.0","id":11,"method":"instances.create","params":{"model":"EXACT_MODEL_ID","workspace_path":"/existing/workspace","idempotency_key":"chat-1"}}
{"jsonrpc":"2.0","id":12,"method":"messages.create","params":{"instance_id":"SAVED_INSTANCE_ID","content":"Hello","idempotency_key":"message-1"}}
{"jsonrpc":"2.0","id":13,"method":"instances.events","params":{"instance_id":"SAVED_INSTANCE_ID","after_seq":0,"limit":100}}
{"jsonrpc":"2.0","id":14,"method":"turns.get","params":{"turn_id":"SAVED_TURN_ID","include_usage":true,"include_error":true}}
```

For a pinned route, add `"account_ref":"team-codex-1"` to
`instances.create`. For automatic routing within one provider, add
`"provider":"codex"` instead. Do not combine them. Creation returns
`instance_id`, `routing_mode`, `routing_provider`, `account_ref`, `model` and
other instance fields; its initial account may change before a later turn.
`messages.create` returns `turn_id`, `message_id`, `instance_id`, `state`,
`replayed` and the account actually selected for that turn in `account_ref`.

`instances.events` and `turns.events` return bare arrays of events with `seq`,
`turn_id`, `instance_id`, `message_id`, `engine`, `kind`, `at`, `data` and
`final`. The event `engine` is `codex` for new v2 turns; provider is route
evidence. Persist the largest committed `seq` and request the next page with
`after_seq`. The `route.selected` data can include `account_changed`,
`portable_context_used` and `context_omitted_count`. `final: true` on
`message.completed` does not finish a turn; wait for `run.finished` or check
`turns.get.state`. `instances.events` does not support `follow: true`.

Per-turn `context_window` is a positive numeric token count. Enable that UI
control only when the observed model metadata provides a maximum, and accept
that the provider may still reject it. Set per-turn `effort` only to a value
reported for the selected model and route. Per-turn `permission_mode` supports
`dontAsk` and `default`; with `default`, process `permission.required` and
respond through `permissions.respond` for the exact request. There is no
`context_package` or `mcp` parameter in v2 `messages.create` yet.

## Errors and uncertain work

Read `error.data.code`, `category`, `phase`, `outcome` and `retryable` from a
JSON-RPC error response. A numeric JSON-RPC code or message text is not a
stable product decision. `outcome: "unknown"` requires inspection and an
explicit user or host recovery decision; it never authorizes a new account or
an automatic rerun. `turns.stop` explicitly cancels a known turn; `recover`
classifies a lost worker. See [errors.md](errors.md) and
[events.md](events.md) for their limits.

## Remove a local account route

After all active turns and login attempts finish, Fullbrain can issue this
independent request:

```json
{"jsonrpc":"2.0","id":15,"method":"accounts.delete","params":{"account_ref":"team-codex-1"}}
```

The result contains `account_ref`, `removed: true` and
`upstream_credential_removed: false`. Historical conversations and evidence
remain readable. This retires AgentBridge's local route; it does not revoke
the upstream credential stored by CLIProxyAPI.
