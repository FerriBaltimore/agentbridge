# Interface operations

Parameters ending in ? are optional. Methods use plural_resource.verb names;
CLI options use --kebab-case. Metadata and provider_options are accepted only
where the implementation declares support.

## Accounts and models

    capabilities.get(account_ref?, refresh?, include_parameters?)
    accounts.list(authentication?, limit?, cursor?)
    accounts.status(account_ref?, account_id?, refresh?)
    accounts.delete(account_ref?, account_id?)
    accounts.login.start(provider, name, request_key?, owner_ref?, email?,
                         mode?, browser?, proxy_base_url?, key_env?,
                         management_key_env?)
    accounts.login.status(attempt_id, owner_ref?, account_ref?)
    accounts.login.check(attempt_id, owner_ref?, account_ref?)
    accounts.login.complete(attempt_id, owner_ref?, account_ref?)
    accounts.login.cancel(attempt_id, owner_ref?, account_ref?)
    accounts.login.callback(attempt_id, owner_ref, redirect_url)
    accounts.usage(account_ref, refresh?)
    accounts.usage_history(account_ref, since?, until?, granularity?,
                           limit?, cursor?, refresh?)
    models.list(account_ref?, refresh?, include_hidden?,
                include_deprecated?, limit?, cursor?)

The login methods are one asynchronous onboarding flow. `login.start` returns
an opaque `attempt_id` and `owner_ref`; the attempt remains queryable after an
AgentBridge restart. AgentBridge launches an empty, dedicated local CLIProxyAPI
sidecar by default; an absolute `AGENTBRIDGE_CLIPROXY_BIN` path outside
model-writable workspaces is required. Advanced callers may supply all three existing proxy route
references together. GrantBridge coordinates provider OAuth using that
sidecar's Management API. CLIProxyAPI owns and refreshes the upstream
credential in its isolated auth directory. `login.check` verifies one active
credential, stable identity and an observed model catalogue. `login.complete`
repeats required checks and atomically creates the account. An ambiguous,
failed or cancelled attempt cannot be promoted. The blocking `accounts.login`
wrapper runs this same flow. In v2, `mode` must be `browser` and `browser`
must be `same_host`; the other login methods require the returned `owner_ref`
or an account reference to resolve ownership. A remote browser can pass its
one-use Codex or Claude localhost redirect to `accounts.login.callback` for
the same pending attempt. AgentBridge validates its expected state and passes
it transiently through GrantBridge to CLIProxyAPI. Then call `status`,
`check` and `complete` as usual. Grok uses the user code returned by the
proxy. The callback URL contains an OAuth code and must never be stored or
logged by the host. This browser relay has deterministic fixture coverage;
live provider acceptance remains pending.

After binding, `accounts.login.status` and the `attempt` object returned by
`accounts.login.complete` include the stable, non-secret `account_id` from that
login attempt. This ID remains tied to the original account if its human name
is reused later.

Neither API-key values nor OAuth tokens enter public account configuration or the AgentBridge
database. The supervisor generates client and management key values and
delivers them over private local channels during login and execution.

`accounts.delete` requires exactly one of `account_ref` or `account_id`.
`account_ref` selects a unique active account by its human name, including
when a retired account had the same name or internal ID. A reference matching
different active accounts by name and internal ID is ambiguous and rejected.
`account_id` selects the exact account for an explicit retry of a retired
account. The delete acknowledgement includes that stable `account_id`.
`accounts.status(account_id=...)` observes that exact account; a name shared by
multiple retired generations stays ambiguous. Positional SDK status calls
retain legacy reference resolution. Retirement fences new routes. For a managed
sidecar, success requires
durable confirmation that the local proxy stopped. If that stop is unknown,
the route stays fenced and `accounts.status` reports
`retirement.local_proxy_stopped: false` without contacting the proxy.
`retirement.managed_proxy` identifies a sidecar owned by AgentBridge. Deleting
an externally managed proxy account retires its AgentBridge route but does not
stop that external process; status reports `managed_proxy: false` and
`local_proxy_stopped: false`.

Historical instances, turns and observations remain readable; active turns and
pending login attempts block retirement. The operation returns
`upstream_credential_removed: false` because AgentBridge does not revoke the
CLIProxyAPI OAuth credential. A new login can reuse the account name. Its
managed sidecar uses a separate account directory and a dynamically assigned
local port.

`models.list` aggregates exact model IDs from proxy accounts. Its
`candidate_account_refs` are account declarations; `observed_account_refs`
have fresh local proxy evidence. `configured_unverified` and `proxy_observed`
do not prove live model entitlement or remaining quota. There is no engine
selector or independent native catalogue route in v2.

## Usage

    usage.get(scope, account_ref?, instance_id?, turn_id?, refresh?,
              since?, until?, include_quota?)
    usage.history(account_ref, since?, until?, granularity?, limit?, cursor?,
                  refresh?)

Scope is account, instance or turn. Usage and quota are separate objects;
monetary cost is returned only when its source is known. Missing or stale
usage is unknown, never zero. `accounts.usage` and `usage.get(scope="account")`
read the same proxy quota surface. Account history methods return arrays;
historical time filters and aggregation are unsupported by this adapter.

## Instances

    instances.create(model, account_ref?, provider?, workspace_path?, effort?,
                     context_window?, permission_mode?, sandbox_mode?,
                     allowed_tools?, continuity_mode?, provider_options?,
                     metadata?, idempotency_key?, evaluation?)
    instances.get(instance_id, include_last_turn?, include_usage?)
    instances.list(account_ref?, state?, limit?, cursor?, include_last_turn?)
    instances.update(instance_id, model?, effort?, context_window?,
                     permission_mode?, sandbox_mode?, allowed_tools?,
                     expected_version?, metadata?)
    instances.archive(instance_id, expected_version?)
    instances.discard_evaluation(instance_id, account_ref?)

Without `account_ref`, AgentBridge chooses an eligible proxy account for the
exact model after fresh local Management API verification. An optional
`provider` limits that automatic choice to accounts of the named provider on
every turn. A supplied `account_ref` pins the route, cannot be combined with
`provider`, and requires the same verification and management key. The route
must have one active credential, a stable identity, the model
in the local catalogue and an available client key. Historical direct accounts
cannot create executable instances. Creation validates declared support before
writing durable state; it does not prove live entitlement or spend a model turn.
`idempotency_key` makes lost-response retries safe.
The signature above includes target optional controls. The current v2 adapter
rejects nondefault effort, context-window, permission, sandbox and tool
controls on `instances.create`; send supported controls with each
`messages.create` call instead. `instances.update` currently changes only
model or state; its listed advanced defaults are unsupported.

`evaluation: true` marks a fresh, disposable instance. Such an instance accepts
one turn through `messages.create` with a validated context package whose
`execution_mode` is `evaluation_inputs_only`. An identical idempotent turn
replay is readable until discard; a new turn and instance transfer are refused.
After the turn and its owned processes end, `instances.discard_evaluation`
removes the instance, turn, event and private native-home data. It returns
`{instance_id, discarded: false, pending: true}` while a run or owned process
may still execute. A completed discard returns `discarded: true, pending:
false` on every retry. A minimal receipt remains so the creation key cannot
recreate or rerun that evaluation. `instances.get` then returns `not_found`.

## Messages and turns

    messages.create(instance_id, content, attachments?, model?, effort?,
                    context_window?, permission_mode?, sandbox_mode?,
                    allowed_tools?, max_turns?, max_budget?, timeout_ms?,
                    context_package?, mcp?, provider_options?, metadata?,
                    idempotency_key?)
    messages.list(instance_id, after?, before?, role?, limit?, cursor?)
    instances.events(instance_id, after_seq?, limit?, follow?, timeout_ms?)
    turns.list(instance_id?, state?, limit?, cursor?)
    turns.get(turn_id, include_usage?, include_error?)
    turns.events(turn_id, after_seq?, limit?, follow?, timeout_ms?)
    turns.stop(turn_id, reason?, grace_period_ms?, wait?)
    turns.resume(turn_id, content?, mode?, timeout_ms?)
    permissions.respond(turn_id, permission_id, decision, reason?, expires_at?)

`messages.create` continues a conversation. On an automatic instance, the
account may change before a new turn. The selected route stays fixed through
the turn and its tool calls. A change starts a fresh Codex thread with bounded
portable context and explicit omissions. Historical direct instances remain
readable but cannot submit another turn. `turns.resume` is explicit recovery;
it never replays an uncertain side effect or changes account silently.

`instances.events` is the incremental conversation feed. Clients should
page with `after_seq` and persist their cursor after consuming the page.
The v2 adapter accepts a positive numeric per-turn `context_window`, subject
to observed model metadata and live provider behavior. It rejects
`allowed_tools`, `max_budget`, `provider_options` and arbitrary metadata.
`context_package` and `mcp` use the bounded private execution contract in
[context-and-mcp.md](context-and-mcp.md). See
[interactive-inputs.md](interactive-inputs.md) for attachments and one-use
permission responses.

## Continuity

    instances.transfer(instance_id, target_account_ref,
                       target_workspace_path?, model?, mode?,
                       validate_only?, budget_bytes?, idempotency_key?)
    instances.export(instance_id, budget_bytes?, include_events?,
                     include_unknowns?)
    recover(instance_id?, turn_id?)

Transfer uses bounded portable context between proxy accounts and keeps the
source independent. Native cross-account transfer is unavailable. Execution
at the destination still requires a verified proxy account. A repeated
`idempotency_key` returns the previously created destination.

## Shared response and concurrency rules

Resource responses expose durable identifiers and source timestamps when
available. `messages.create` returns distinct `turn_id` and `message_id`.
`models.list` returns `items`, `next_cursor` and `has_more`. A missing provider
value is marked unsupported or stale, never invented.

A second active turn for an instance is rejected unless the capability
explicitly allows it. Updates use `expected_version`. Archived instances
remain readable until the host's retention policy removes them; archive does
not delete data.
