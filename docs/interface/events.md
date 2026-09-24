# Event contract

For new v2 turns, `engine` is always `codex`; upstream provider and selected
account are separate route evidence. Older direct events remain readable.

Every public event has:

    seq, instance_id, message_id, turn_id, engine, kind, at, data, final

`seq` is monotonic per store. Clients resume using `after_seq`, not a provider
cursor. An unknown provider event uses `provider.event` after safe projection;
a known unreadable or lost segment uses `recovery.gap`. The adapter must not
invent a successful result.

## Python live iterator

`Bridge.turn_events_stream(turn_id, after_seq=0, timeout_ms=None)` yields the
same public dictionaries as `Bridge.turn_events`. It first replays saved events
with `seq > after_seq`, then follows new observations in store order. The
iterator uses bounded pages and does not depend on an upstream provider's
stream format. Persist the last consumed `seq` and pass it as `after_seq` when
reconnecting; replay does not create another provider turn.

The iterator stops after a terminal turn and its saved events have been
drained. `timeout_ms` is an optional idle limit: it resets after each yielded
event, `0` drains currently saved events without waiting, and `None` waits
until terminal. An idle timeout returns normally while the turn may still be
active; read `turns.get.state` before reporting completion. Closing the
iterator stops observation only. Use `turns.stop` for explicit cancellation.

## Bounded long polling

`Bridge.turn_events(turn_id, after_seq=..., limit=..., follow=True,
timeout_ms=...)` and the JSON-RPC `turns.events` method return one available
page immediately. When no event is available, they wait for the first saved
event and return its page. An idle timeout or a drained terminal turn returns
an empty list. Empty does not mean complete: read the turn state separately.
Continue with the last returned `seq` to avoid duplicates. With `follow=False`,
the method reads one available snapshot without waiting. The lower-level
`Run.events(follow=True)` iterator keeps its legacy timeout exception.

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
