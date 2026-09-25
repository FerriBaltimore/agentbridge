# Persistent message queues

AgentBridge owns one ordered queue per conversation. The SDK, CLI and JSON-RPC
share the same SQLite records and concurrency rules. A detached local
dispatcher continues accepted work after the submitting client disconnects.
No host UI loop is needed to launch the next message.

## Operations

```text
queues.list(instance_id, limit?, cursor?)
queues.add(instance_id, content, position?, expected_version?, idempotency_key?,
           ...messages.create input controls)
queues.move(instance_id, message_id, position, expected_version?)
queues.delete(instance_id, message_id, expected_version?)
queues.dispatch(instance_id, message_id, mode, expected_version?, expected_turn_id?)
queues.pause(instance_id, expected_version?)
queues.resume(instance_id, expected_version?, message_id?, context_package?, mcp?)
messages.get(message_id)
```

Python methods are `Bridge.queue_list`, `queue_add`, `queue_move`,
`queue_delete`, `queue_dispatch`, `queue_pause`, `queue_resume` and
`message_get`. These are the SDK equivalents of the public resource methods.

`queues.add` appends by default. `position` is a zero-based position in the
pending list. For insertion, its maximum is the current number of pending
items; for a move, its maximum is one less than that number. Moving shifts the
intervening items. At most 1000 pending messages are accepted per conversation.
Deleting removes an unstarted item from the queue and records it as cancelled;
it does not stop an already dispatched turn or erase its audit evidence.

`queues.list` returns `items`, `total`, `next_cursor`, `has_more`, `version`,
`paused`, `reason`, `dispatcher_running` and `in_flight`. `in_flight` contains
live inputs awaiting provider acknowledgement. Each item exposes `message_id`,
`instance_id`, `content`, attachment descriptors, `position`, `state`,
`delivery`, `turn_id`, `target_turn_id`, `error` and source timestamps.
`context_required` identifies input with an ephemeral private binding.

Queue changes are transactional. `expected_version` rejects competing edits
with `version_conflict`. Dispatch checks the head again in the same transaction
that creates its turn: a concurrent move or removal cannot execute stale input.
An accepted immediate replacement remains first until admitted.
`idempotency_key` identifies the original admission request, including its
content, settings, initial insertion position and delivery mode. Repeating it
returns the same message even after reordering, cancellation or completion.

## Delivery modes

`messages.create` accepts `delivery`:

| Value | Behavior |
| --- | --- |
| `queue` | Persist the message; dispatch it when it reaches the head and the instance is idle |
| `steer` | Deliver additional input to the active turn without starting another execution |
| `interrupt` | Request cancellation of the current turn and start this input ahead of ordinary pending messages once the old processes exit |
| `reject` | Compatibility default: attempt immediate admission and reject an occupied instance |

Use `queues.dispatch(mode="steer" or "interrupt")` to promote an existing
pending message. It keeps its original `message_id`. `expected_turn_id` fences
the operation to the turn the caller observed. Steering requires a live turn;
interruption on an idle instance moves the input to the front for execution.
There is never more than one executing turn for a conversation or account.

Queue admission returns a durable `message_id` immediately. Its `turn_id` and
`account_ref` may be null until actual execution admission. Poll
`messages.get`, or consume `instances.events`, to obtain the assigned turn.
`messages.get.state` follows the turn after dispatch and `queue_state` retains
the queue delivery state. Model settings are captured at enqueue time; automatic
account selection, fresh proxy checks and native version checks happen at
execution admission. Pending input fences instance route changes, archive and
deletion. Remove pending items before performing those operations.

Queue turns use the interactive adapter and can receive steering. Historical
or compatibility turns launched with the one-shot transport return
`steering_unsupported`; they still support explicit interruption. Steering
retains the active account, model, permissions and execution context. Conflicting
settings, fresh context packages and account exclusions are rejected instead
of changing the running turn. Provider-native protocol fields remain internal.
New steering messages inherit omitted controls from the active turn; explicitly
conflicting values are rejected. Promoted queue entries retain their saved controls.
An outstanding tool approval can delay delivery until that interaction ends.

## Observation and recovery

`instances.events` emits durable `queue.changed` events for admission, readiness,
reordering, removal, pause, resume, dispatch and live-input delivery. Pre-turn
events have a null `turn_id`. They share the ordinary monotonic event cursor.
Pending input appears in `messages.list` but is excluded from portable execution
context. A successfully acknowledged steering input appears exactly once with
its own message ID in the transcript and in conversation context.

The delivery states are `staged`, `queued`, `blocked`, `dispatched`,
`steering`, `delivering`, `delivered`, `rejected`, `unknown` and `cancelled`.
`staged` means the input is durable but its private handoff has not completed.
`dispatched` means the new turn was admitted; it does not mean that turn succeeded.
`delivered` means the active-turn input was acknowledged by the native adapter.
A lost acknowledgement is `unknown` and is never automatically resent.

Pause stops admission of subsequent queued work and leaves active work running.
Ordinary Stop pauses an existing queue. A failed, incomplete or interrupted turn
also pauses its pending tail. An explicit replacement can proceed after confirmed
cancellation, but an unknown outcome requires inspection and explicit resume.
Admission failures retain the head as `blocked`, with a safe error code, and
pause the queue. Busy accounts are awaited without creating an execution attempt.

Client disconnection does not stop the dispatcher. If the dispatcher or host
is lost, the queue and its order remain in SQLite. `queues.list` reports whether
its dispatcher process is alive. `queues.resume` reconnects it explicitly;
already admitted turns are observed and never repeated. `recover` retains its
existing observation-only semantics.

Credentials and private context/MCP bindings are never persisted in queue
records or events. The dispatcher receives them through a private local socket.
After dispatcher loss, ordinary input can resume with available credential
references. An input requiring private context becomes `blocked` with
`context_required`; rebind its exact validated context through
`queues.resume(message_id=..., context_package=..., mcp=...)`. It cannot silently
execute without its selected context. Evaluation instances keep their existing
single-turn contract and reject queue admission.

## Examples

```python
bridge.queue_pause(instance_id)
first = bridge.queue_add(instance_id, "Review the change", idempotency_key="review-1")
second = bridge.queue_add(instance_id, "Check the failing test", idempotency_key="test-1")
queue = bridge.queue_list(instance_id)
bridge.queue_move(instance_id, second["message_id"], 0, expected_version=queue["version"])
bridge.queue_resume(instance_id)
# Later, while an interactive turn is active:
bridge.message_create(instance_id, "Use the existing test fixture", delivery="steer")
```

```shell
agentbridge queues list INSTANCE_ID
agentbridge queues add INSTANCE_ID "Review the change" --idempotency-key review-1
agentbridge queues move INSTANCE_ID MESSAGE_ID --position 0
agentbridge queues delete INSTANCE_ID MESSAGE_ID
agentbridge queues dispatch INSTANCE_ID MESSAGE_ID --mode interrupt --expected-turn-id TURN_ID
agentbridge queues pause INSTANCE_ID
agentbridge queues resume INSTANCE_ID
```

The CLI prints JSON. Advanced message settings and private context rebinding
are also available through `agentbridge call queues.add` and
`agentbridge call queues.resume`, which read JSON parameters from stdin.

Deterministic subprocess tests cover queue order, edits, duplicate requests,
concurrent mutations, client/dispatcher loss, cancellation, steering rejection
and lost acknowledgement. The pinned Codex 0.153.0 executable's generated local
schema confirms the native steering request and acknowledgement fields.
These checks establish implementation and fixture coverage; live acceptance
through upstream providers remains separate and pending.
See the [recorded acceptance checks](../development/message-queues-acceptance.md).
