# Interface operations

Parameters ending in ? are optional. messages.create accepts idempotency_key;
instance updates use expected_version. Metadata and provider_options are
accepted only where the adapter declares them.

## Discovery

    capabilities.get(engine?, account_ref?, refresh?, include_parameters?)
    accounts.list(engine?, authentication?, limit?, cursor?)
    accounts.status(account_ref, refresh?)
    accounts.login.start(engine, name, email?, mode?, browser?, request_key?)
    accounts.login.status(attempt_id, owner_ref?, account_ref?)
    accounts.login.check(attempt_id, owner_ref?, account_ref?, inference?)
    accounts.login.complete(attempt_id, owner_ref?, account_ref?)
    accounts.login.cancel(attempt_id, owner_ref?, account_ref?)

The login methods are asynchronous. `login.start` returns an opaque `attempt_id`
and `owner_ref` immediately. The attempt is durable and can be queried after an
AgentBridge restart. An isolated worker owns the native login until it finishes,
even if the caller exits. `login.check` queues verification and returns quickly;
poll status until `checking` is false. `login.complete` requires that verification
to have passed and binds atomically. It never starts a hidden model turn.
`inference: true` explicitly permits a small model verification request, which may
consume usage. Claude requires this because its default check only reads the local
profile. `accounts.login` remains as a bounded
compatibility wrapper for callers that explicitly need a blocking operation.
    models.list(engine, account_ref?, refresh?, include_hidden?,
                include_deprecated?, limit?, cursor?)

models.list returns live data when possible and a marked fallback otherwise.
accounts.login delegates credentials and browser work to GrantBridge.

## Usage

    usage.get(scope, account_ref?, instance_id?, turn_id?, refresh?,
              since?, until?, include_quota?)
    usage.history(account_ref, since?, until?, granularity?, limit?, cursor?,
                  refresh?)

scope is account, instance or turn. Usage and quota are separate objects, and
monetary cost is only returned when its source is known.

## Instances

    instances.create(engine, account_ref, workspace_path?, model?, effort?,
                      context_window?, permission_mode?, sandbox_mode?,
                      allowed_tools?, continuity_mode?, provider_options?,
                      metadata?)
    instances.get(instance_id, include_last_turn?, include_usage?)
    instances.list(engine?, account_ref?, state?, limit?, cursor?,
                   include_last_turn?)
    instances.update(instance_id, model?, effort?, context_window?,
                     permission_mode?, sandbox_mode?, allowed_tools?,
                     expected_version?, metadata?)
    instances.archive(instance_id, expected_version?)

Creation validates account and supported parameters before writing the durable
instance. It does not verify model availability or spend an inference turn. `account_ref` is mandatory and
`idempotency_key` may be supplied to make retries safe across lost responses.

## Messages and turns

    messages.create(instance_id, content, attachments?, model?, effort?,
                    context_window?, permission_mode?, sandbox_mode?,
                    allowed_tools?, max_turns?, max_budget?, timeout_ms?,
                    provider_options?, metadata?)
    messages.list(instance_id, after?, before?, role?, limit?, cursor?)
    instances.events(instance_id, after_seq?, limit?, follow?, timeout_ms?)
    turns.list(instance_id?, state?, limit?, cursor?)
    turns.get(turn_id, include_usage?, include_error?)
    turns.events(turn_id, after_seq?, limit?, follow?, timeout_ms?)
    turns.stop(turn_id, reason?, grace_period_ms?, wait?)
    turns.resume(turn_id, content?, mode?, timeout_ms?)
    permissions.respond(turn_id, permission_id, decision, reason?, expires_at?)

messages.create is the normal way to continue a conversation. turns.resume is
for explicit recovery and never repeats an unknown side effect silently.
`instances.events` is the incremental conversation feed. Fullbrain should page
with `after_seq` and keep its own cursor; `follow=true` is intentionally not
used by the first adapter.

See [interactive-inputs.md](interactive-inputs.md) for the implemented attachment
formats, per-provider permission modes and durable one-use response semantics.

## Continuity

    instances.transfer(instance_id, target_account_ref,
                       target_workspace_path?, model?, mode?,
                       validate_only?, budget_bytes?, idempotency_key?)
    instances.export(instance_id, budget_bytes?, include_events?,
                     include_unknowns?)
    recover(instance_id?, turn_id?)

Transfer reports native, portable, unsupported or unverified and keeps the
source instance independent. A supplied `idempotency_key` makes a retry return
the already-created destination instance without copying or exporting again.

## Shared response fields

Resource responses expose their durable identifiers and observed timestamps
when the source has them. messages.create always returns distinct turn_id, message_id,
instance_id, state and replayed. models.list returns items, next_cursor and
has_more. Legacy account and transcript collections remain list-shaped for
compatibility and accept bounded limit and cursor arguments. A provider value
that is not available is marked unsupported or stale, never invented.

## Concurrency and retention

The store rejects a second active turn for an instance unless the capability
explicitly declares parallel turns. Updates accept expected_version to prevent
lost writes. Archived instances remain readable until the host's retention
policy removes them. Deletion is a separate administrative operation and is
never implied by archive.
