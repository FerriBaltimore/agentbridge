# The surface Fullbrain v2 uses

One page for the Fullbrain v2 adapter (`backend/fullbrain/adapters/agentbridge`): every method
it calls, the request and response shape it relies on, and the AgentBridge version in which
that shape appeared. Wider contracts stay in [operations.md](operations.md),
[events.md](events.md), [message-queues.md](message-queues.md), [errors.md](errors.md) and
[mobile-login.md](mobile-login.md); worked JSON examples are in
[fullbrain-v2-rpc.md](fullbrain-v2-rpc.md). The numbers a consumer compares before starting a
worker are in [../compatibility.md](../compatibility.md).

Each entry ends with the version in which its shape appeared. `2.0.0` is the v2 baseline
(shapes inherited from the `0.1.x` direct adapters count as `2.0.0`). `pin` marks shapes added
after the `2.3.3` metadata bump that Fullbrain v2 already runs through its pinned artifact
`a7ac24a5…` (v2 label `2.4.2`); `2.5.0` is the first tagged version that carries them. Every
shape here is fixture tested; live provider acceptance is tracked in
[provider-acceptance.md](provider-acceptance.md).

## Transport and handshake

- Process: `agentbridge --root PRIVATE_STATE_DIR rpc`; JSON-RPC 2.0, one object per line on
  stdin/stdout, no network listener. Request `{jsonrpc: "2.0", id, method, params: {…}}` with
  `params` always an object. — 2.0.0
- Errors: JSON-RPC `error.code` `-32000`; the stable code is `error.data.code`
  (§ Errors). — 2.0.0
- Handshake: call `capabilities.get` first and refuse `contract_version != "v2"`. — 2.0.0
- `agentbridge --version` prints the package version (`2.5.0`). — 2.0.0
- Python SDK (`agentbridge.Bridge`) methods mirror the RPC names: `capabilities`,
  `account_login_start`, `account_login_status`, `account_login_check`,
  `account_login_complete`, `account_login_cancel`, `account_login_callback`,
  `instance_create`, `instance_update`, `message_create`, `message_get`, `turn_get`,
  `turn_events`, `turn_stop`, `queue_list`, `queue_move`, `queue_delete`, `queue_dispatch`,
  `queue_pause`, `queue_resume`. v2's legacy login worker calls
  `Bridge.account_login_start(**options)` with the RPC parameter names. — 2.0.0

## Capabilities

`capabilities.get(account_ref?, refresh?, include_parameters?)` returns an object with:

- `contract_version: "v2"`, `declaration_scope: "adapter_implementation"`,
  `execution_engine: "codex"`, `route: "local_cliproxyapi"`,
  `providers: ["codex", "claude", "grok"]`, `runtime_provider_support_verified: false`
  until live acceptance is recorded. — 2.0.0
- `operations: {name: {support, maturity, limitations[]}}`. v2 enables a method only when
  `support` is `native` or `adapter` and `maturity` is `implemented`, `fixture_tested` or
  `provider_tested`. — 2.0.0; `queues.*` and `messages.get` entries — pin
- `parameters`: `routing_mode`, `model`, `effort`, `attachments`, `context_window`,
  `context_package`, `mcp`, `permission_mode`, `sandbox_mode`, each with `support`,
  `maturity`, `values?`, `scopes?`, `limitations[]`; `allowed_tools`, `max_budget` and
  `provider_options` are `unsupported`. v2 disables `messages.create` unless
  `parameters.context_package.support == "adapter"`. — 2.0.0; `delivery` — pin
- `accounts.login*` limitations read `same_host_isolated_or_mobile_browser`,
  `hosted_browser_requires_grantbridge_host`, `live_oauth_acceptance_pending`. — 2.5.0

## Accounts and models

- `accounts.list(authentication?, limit?, cursor?)` → bare array of `{account_ref, name,
  email, provider?, supported_models?, authentication, routing, identity, reason?}`; `[]` on
  a fresh state root. — 2.0.0
- `accounts.status(account_ref | account_id, refresh?)` (exactly one reference) →
  `{account_id, configured{provider, supported_models, credential_ref}, routing{paused,
  paused_at}, authentication{status, source, observed_at}, identity{…},
  retirement?{managed_proxy, local_proxy_stopped}, reason?}`. — 2.0.0; `routing` — 2.3.3
- `accounts.usage(account_ref, refresh?)` → `{scope: "account", supported, stale, reason,
  source?, quota_windows?[], refresh_reason?}`; each window `{id, label, scope, model_id?,
  used_percent, remaining_percent, window_seconds?, resets_at?, observed_at, stale_at, stale}`.
  `refresh: true` performs one bounded upstream quota read through the bound proxy; a failed
  refresh keeps the last observation and its age. Unknown is never zero. — 2.0.0; windows,
  `stale_at`, `refresh_reason` — 2.3.2
- `accounts.pause(account_ref | account_id)`, `accounts.resume(...)` → `{account_ref,
  routing{paused, paused_at}}`; idempotent; paused accounts leave the routable catalogue and
  keep login, quota, history and admitted turns. — 2.3.3
- `accounts.delete(account_ref | account_id)` → `{account_ref, account_id, removed: true,
  upstream_credential_removed: false}`; `busy` while a turn or login attempt is active;
  `unknown_outcome` when the sidecar stop is unconfirmed (then `accounts.status` reports
  `retirement.local_proxy_stopped: false`). — 2.0.0
- `models.list(provider?, account_ref?, refresh?, include_hidden?, include_deprecated?,
  limit?, cursor?)` → `{source: "agentbridge_routing", stale, models[], items[], next_cursor,
  has_more}`; item `{id, display_name, availability, source, providers,
  candidate_account_refs, observed_account_refs, reasoning_efforts, context_windows,
  input_modalities, account_capabilities}`. Empty metadata arrays mean unknown controls.
  — 2.0.0; metadata arrays — 2.3.1
- References: when two active accounts share a name, results use the stable
  `id:<account_id>` form and an unqualified ambiguous name is rejected. — pin

## Login

`accounts.login.start` request (`login_start` protocol `2`):

- `provider` (`codex`, `claude`, `grok`), `name` (unique, preserved as entered),
  `request_key?` (a replay returns the same attempt instead of a second OAuth), `owner_ref?`
  (1–128 chars), `email?` (expected identity when the provider returns none),
  `proxy_base_url?` + `key_env?` + `management_key_env?` (all three, for an external sidecar;
  environment variable names, never values). — 2.0.0
- `browser?`: `same_host` (default), `isolated`, `mobile`; any other value fails with
  `invalid_request`. `mode?`: `browser` (default), `hosted`; `hosted` requires `mobile`,
  otherwise `unsupported_operation`. For `mobile`, `owner_ref` is the phone's GrantBridge
  owner digest and binds the viewer and callback route. — 2.5.0

Attempt projection, returned by `start`, `status`, `check`, `cancel` and as `complete.attempt`:

- `attempt_id`, `owner_ref` (opaque; persist both before showing the challenge),
  `account_ref` (the name until bound, then the account reference), `provider`, `status`,
  `authorization_url?` (https, ≤ 16384 chars), `user_code?` (Grok), `created_at?`,
  `updated_at?`, `expires_at?`, `identity?`, `verification?`, `error?{code}`. — 2.0.0
- `status` values: `starting`, `awaiting_user`, `exchanging`, `authorized`, `verified`,
  `bound`, `failed`, `cancelled`, `expired`, `interrupted`, `abandoned`, `revoked`,
  `replaced`; treat unknown values conservatively. — 2.0.0
- `account_id` once the attempt is `bound`. — 2.0.0
- `browser` and `mode` echoed as sent. `viewer_url?` (https ≤ 2048, http only on loopback)
  replaces `authorization_url` when `mode` is `hosted`. — 2.5.0
- `identity.email` on `failed` with `error.code: identity_changed`: the identity the provider
  returned when it differs from the requested `email`; the credential is retired. — 2.5.0

Entries and the host's part:

- `same_host` / `browser`: open `authorization_url` in any browser on the sidecar host. — 2.0.0
- `isolated` / `browser`: same wire request; while the attempt is `awaiting_user` the host
  opens the URL through `agentbridge.auth_browser.IsolatedAuthBrowser` (fresh disposable
  Chromium profile, stdlib only) and stops it when the phase ends. `available()` is false
  without a display or Chromium, so v2 launches it from the API process on the brain host,
  never from the sandboxed worker, and falls back to `same_host`. — 2.5.0
- `mobile` / `browser`: the phone opens `authorization_url`; the dead loopback redirect
  returns through `accounts.login.callback` or GrantBridge's owner-bound
  `GET|POST /oauth/proxy/callback`. — 2.5.0
- `mobile` / `hosted`: the phone opens `viewer_url` on the GrantBridge origin; needs a
  long-lived GrantBridge host, which is where `grantbridge_min` applies. — 2.5.0

Failures before dispatch: `oauth_callback_port_busy`, `oauth_callback_unavailable` (2.0.0);
`hosted_browser_unavailable`, `invalid_browser` (2.5.0). A GrantBridge that does not echo the
mobile entry is cancelled and the attempt fails with `provider_protocol_error` (2.5.0). A
hosted login can end `failed` with `browser_busy`, `browser_closed` or
`hosted_browser_unavailable` (2.5.0).

Other login methods, each taking `attempt_id` plus `owner_ref` or `account_ref`:

- `accounts.login.status` → current projection; poll while `starting`, `awaiting_user` or
  `exchanging`. — 2.0.0
- `accounts.login.check` → fresh sidecar identity and model check after `authorized`; fails
  with `identity_changed` on a mismatch (read `status` for `identity.email`). — 2.0.0
- `accounts.login.complete` → repeats the checks and binds atomically:
  `{account{public account}, attempt, identity}`. — 2.0.0
- `accounts.login.cancel` → explicit abandonment; `already_finished` when OAuth completed;
  never claims the upstream credential was deleted. — 2.0.0
- `accounts.login.callback(attempt_id, owner_ref, redirect_url)` → the one-use Codex or Claude
  loopback redirect, relayed in memory to the same pending attempt; then poll `status`. — 2.0.0
- `accounts.login.list(provider?, limit?, cursor?)` → at most 100 interrupted attempts, newest
  first, with `attempt_id` and `owner_ref` so a trusted host can cancel them. — 2.3.0

The callback URL, OAuth code and proxy keys never enter SQL, receipts, logs, browser storage
or error text. An interrupted OAuth has an unknown remote outcome; never start a second
attempt automatically.

## Instances

- `instances.create(model, workspace_path, idempotency_key?, account_ref?, provider?,
  routing_mode?, permission_mode?, sandbox_mode?, excluded_account_refs?, evaluation?)` →
  `{instance_id, model, routing_mode, routing_provider, affinity_account_ref, account_ref,
  version, state, …}`. — 2.0.0; `affinity_account_ref` and access defaults — pin
- `instances.get(instance_id, include_last_turn?, include_usage?)` → instance fields;
  `instance_not_found` after deletion or discard. — 2.0.0
- `instances.update(instance_id, model?, provider?, routing_mode?, account_ref?,
  permission_mode?, sandbox_mode?, expected_version?)` → the updated instance;
  `version_conflict` on a stale version; refused while a turn or queue work is pending; the
  native Codex session is retained. — 2.0.0; access defaults — pin
- `instances.discard_evaluation(instance_id)` → `{instance_id, discarded, pending}`; repeat
  while `pending`; only for `evaluation: true` instances. — 2.0.0
- `instances.events(instance_id, after_seq?, limit?)` → bare array of events (§ Events);
  `follow` is not supported here. — 2.0.0

Rules v2 relies on: an omitted `account_ref` selects automatic routing and a supplied one
pins; `routing_mode: "automatic"` with `account_ref` sets a durable affinity; `provider`
filters automatic routing; a pinned route cannot use exclusions; `excluded_account_refs`
holds up to 1000 distinct references and is honoured on creation and on every automatic
turn; the instance binds once to one native Codex session that survives model, provider and
account changes; an `idempotency_key` replay returns the same instance and route choice.

## Messages, turns and events

- `messages.create(instance_id, content, idempotency_key?, attachments?, model?, effort?,
  context_window?, permission_mode?, sandbox_mode?, context_package?, mcp?,
  excluded_account_refs?, execution_mode?)` → `{turn_id, message_id, instance_id, state,
  replayed, account_ref}`; the account actually selected for the turn. — 2.0.0
- Queued delivery: `delivery: "queue" | "steer" | "interrupt"`, `position?`,
  `expected_version?`, `expected_turn_id?`; `turn_id` and `account_ref` may be `null` until
  admission. The compatibility default `reject` refuses a busy instance. — pin
- `messages.get(message_id)` → the message with `state`, `queue_state`, `delivery`, `turn_id`,
  `target_turn_id`, `error`. — pin
- `turns.get(turn_id, include_usage?, include_error?)` → `{turn_id, instance_id, message_id,
  state, outcome, account_ref, created_at, updated_at, provider_compatibility, usage?,
  error?, error_detail?}`. — 2.0.0
- `turns.events(turn_id, after_seq?, limit?, follow?, timeout_ms?)` → bare array; one page
  immediately, or the first saved event when following; empty on an idle timeout, which does
  not mean complete. — 2.0.0; bounded long polling — 2.3.1
- `turns.stop(turn_id, reason?, wait?)` → the turn snapshot; explicit cancellation only, no
  rerun. — 2.0.0

Event envelope: `{seq, instance_id, message_id, turn_id, engine: "codex", kind, at, data,
final}`. Persist the largest committed `seq` and page with `after_seq`. — 2.0.0. Normalized
kinds — 2.3.1: `message.created`, `run.started`, `session.started`, `message.delta`,
`message.completed`, `tool.started`, `tool.completed`, `subagent.status`, `usage.observed`,
`quota.observed`, `context.compacting`, `context.compacted`, `model.changed`,
`route.selected`, `run.retrying`, `permission.required`, `permission.denied`,
`permission.responded`, `run.error`, `run.finished`, `recovery.observed`, `recovery.gap`,
`provider.event`. `queue.changed` — pin: `data.version`, `data.action`, `turn_id` null before
admission. `final: true` on `message.completed` does not end a turn; `run.finished` or
`turns.get.state` does. `route.selected.data` may carry `account_changed`,
`portable_context_used` and `context_omitted_count`. A tool result with `outcome: unknown` is
never counted as success.

Per-turn controls: `context_window` is a positive token count accepted only when the
selected account's observed model ceiling covers it (`context_window_unavailable`
otherwise) — 2.0.0; `permission_mode` `dontAsk | default` and `sandbox_mode` `read-only |
workspace-write | danger-full-access` (full access is incompatible with `context_package`
or `mcp`) — 2.0.0 per turn, defaults pin; `context_package` `{version 1 | 2, selection_hash,
instructions, evidence, exclusions, …}` — 2.0.0, bounded to 1 MiB of instructions and 1.5 MiB
in total — pin; `mcp` `{version: 1, socket_path, operation_id, capability}` over a private
Unix socket — 2.0.0. Evaluation instances take exactly one turn with
`execution_mode: "evaluation_inputs_only"` — 2.0.0.

## Queues — pin

- `queues.list(instance_id, limit?, cursor?)` → `{items[], total, next_cursor, has_more,
  version, paused, reason, dispatcher_running, in_flight[]}`; item `{message_id, instance_id,
  content, position, state, delivery, turn_id, target_turn_id, error, context_required, …}`.
- `queues.move(instance_id, message_id, position, expected_version?)` and
  `queues.delete(instance_id, message_id, expected_version?)` → queue snapshot with the new
  `version`; a deleted item is recorded `cancelled`.
- `queues.dispatch(instance_id, message_id, mode: "steer" | "interrupt", expected_version?,
  expected_turn_id?)` → the promoted message, keeping its `message_id`.
- `queues.pause(instance_id, expected_version?)`, `queues.resume(instance_id,
  expected_version?, message_id?, context_package?, mcp?)` → queue snapshot; resume rebinds a
  `context_required` input with its exact validated context.
- Delivery states: `staged`, `queued`, `blocked`, `dispatched`, `steering`, `delivering`,
  `delivered`, `rejected`, `unknown`, `cancelled`. `expected_version` rejects competing edits
  with `version_conflict`; queue errors after a durable admission carry `details.message_id`.

## Errors

Envelope — 2.0.0: `error.data = {code, category, retryable, action, phase, outcome,
retry_after_ms, details}`; `outcome: "unknown"` is never retryable and never authorises a
rerun or another account. Codes the v2 adapter maps explicitly:

- Requests — 2.0.0: `invalid_request`, `unsupported_operation`, `unsupported_parameter`,
  `version_conflict`, `idempotency_conflict`.
- Accounts — 2.0.0: `account_not_found`, `account_busy`, `busy`, `authentication_required`,
  `account_unavailable`, `quota_exhausted`, `rate_limited`.
- Login — 2.0.0: `authentication_attempt_not_found`, `authentication_owner_required`,
  `authentication_attempt_not_ready`, `authentication_not_verified`,
  `authentication_in_progress`, `authentication_outcome_unknown`, `login_timeout`,
  `identity_changed`, `oauth_callback_port_busy`, `oauth_callback_unavailable`.
- Login entries — 2.5.0: `hosted_browser_unavailable`, `invalid_browser`, `browser_busy`,
  `browser_closed`.
- Routing — 2.0.0: `model_not_found`, `model_unavailable`, `model_required`,
  `proxy_binding_unverified`, `proxy_binding_changed`, `proxy_endpoint_shared`,
  `context_window_unavailable`, `context_stale`.
- Turns — 2.0.0: `instance_not_found`, `instance_busy`, `turn_not_found`,
  `provider_timeout`, `provider_unavailable`, `provider_protocol_error`, `provider_failed`,
  `native_session_missing`, `native_session_diverged`, `cancelled`, `interrupted`,
  `unknown_outcome`, `store_unavailable`, `permission_required`, `permission_denied`.
- Execution access — pin: `invalid_execution_policy`.
- Queues — pin: `queue_full`, `invalid_position`, `message_not_found`, `message_not_pending`,
  `message_dispatching`, `interrupt_pending`, `queue_dispatcher_unavailable`,
  `queue_dispatch_failed`, `invalid_delivery`, `turn_conflict`, `turn_not_active`,
  `steering_unsupported`, `steering_rejected`, `context_required`,
  `evaluation_queue_unsupported`.

Unknown codes map to the generic failure; unknown login states and event kinds are handled
conservatively and never imply success.

## Version history of this surface

- 2.0.0 (`d69deab`, `422ba55`, `7d925db`): the v2 contract, GrantBridge login,
  `accounts.login.callback`, automatic and pinned routing, `instances.update`, evaluation
  instances, `context_package`, `mcp`, `excluded_account_refs`, `account_id` on bound attempts.
- 2.1.0 (`95dd254`): bundled Linux runtimes in the wheel.
- 2.2.0 (`9f6f511`): route changes between turns exercised end to end from the playground.
- 2.3.0 (`7efb0de`): `accounts.login.list`.
- 2.3.1 (`39b2bb7`): model metadata arrays, normalized event kinds, bounded long polling.
- 2.3.2 (`fe2d829`): `quota_windows`, `stale_at`, `refresh_reason`.
- 2.3.3 (`6ffbede`): `accounts.pause`, `accounts.resume`, `routing.paused`.
- pin, v2 label `2.4.2` (`73a9c2e`, `32e3cbd`): queues, `messages.get`, access defaults,
  `instances.delete`, affinity, `id:<account_id>` references, context package limits.
- 2.5.0 (`14aa9da`, `4bdc4d0`): `browser` `isolated` and `mobile`, `mode` `hosted`,
  `viewer_url`, echoed `browser` and `mode`, `identity.email`, the new login error codes, the
  release manifest and `agentbridge.protocol_versions`.
