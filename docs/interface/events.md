# Event contract

Every stored event has:

    seq, instance_id, message_id?, turn_id, engine, kind, at, data, final

seq is monotonic per store. Clients resume a stream using after_seq, not a
provider cursor. Provider fields that are not understood become a gap event;
the adapter must not invent a successful result.

## Common kinds

    run.accepted
    run.started
    session.started
    message.delta
    message.completed
    tool.started
    tool.completed
    subagent.status
    usage.observed
    quota.observed
    permission.required
    permission.denied
    warning
    error
    run.finished
    recovery.observed
    recovery.gap
    provider.event

The existing legacy event names remain readable during migration. New methods
use these normalized names. Unknown provider events use provider.event and
keep only redacted adapter data.

## Terminal behavior

run.finished is committed with the terminal turn state. A lost worker emits a
recovery event, marks unfinished tool results as unknown, and finishes as
interrupted. No provider request is rerun automatically.

## Permissions

permission.required contains the bounded action and tool identifier. It never
contains a secret, private reasoning or an unbounded provider payload. The host
decides whether and how a permission is granted.
