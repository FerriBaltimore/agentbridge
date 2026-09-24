# Event contract

For new v2 turns, `engine` is always `codex`; upstream provider and selected
account are separate route evidence. Older direct events remain readable.

Every public event has:

    seq, instance_id, message_id, turn_id, engine, kind, at, data, final

`seq` is monotonic per store. Clients resume using `after_seq`, not a provider
cursor. An unknown provider event uses `provider.event` after safe projection;
a known unreadable or lost segment uses `recovery.gap`. The adapter must not
invent a successful result.

## Normalized kinds

    message.created
    run.started
    session.started
    message.delta
    message.completed
    tool.started
    tool.completed
    subagent.status
    usage.observed
    quota.observed
    context.compacting
    context.compacted
    model.changed
    route.selected
    run.retrying
    permission.required
    permission.denied
    permission.responded
    run.error
    run.finished
    recovery.observed
    recovery.gap
    provider.event

The existing legacy event names remain readable during migration. New methods
use these normalized names. `route.selected` records the account decision
before execution and may include `account_changed`, `portable_context_used`
and `context_omitted_count`. Read `messages.create.account_ref` or
`turns.get.account_ref` for the selected public account reference.
`context.compacting` is emitted when Codex reports that compaction has started;
`context.compacted` confirms its completion. Providers that report only a
completion boundary cannot supply the starting observation.

## Terminal behavior

`final` marks the end of one complete message or a finished run. It is true
for `run.finished` and can also be true for `message.completed`; consumers
must not treat every `final: true` event as a terminal turn. `run.finished` is
committed with the terminal turn state; `turns.get.state` is an authoritative
snapshot. A lost worker emits a recovery event, marks unfinished tool results
as unknown, and finishes as interrupted. No provider request is rerun
automatically.

## Permissions

`permission.required` contains the bounded action and tool identifier. It
never contains a secret, private reasoning or an unbounded provider payload.
The host decides whether and how a permission is granted.
