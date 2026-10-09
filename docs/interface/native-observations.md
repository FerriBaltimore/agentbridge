# Native thread observations

`instances.read(instance_id, include_turns=true)` and
`instances.reopen(instance_id, include_turns=true)` read Codex itself. The corresponding
Python methods are `Bridge.instance_read` and `Bridge.instance_reopen`.

Both return:

```json
{
  "instance_id": "sdk-instance",
  "native_session_id": "native-thread",
  "source": "native",
  "status": {"type": "active", "active_flags": []},
  "turns": [{
    "native_turn_id": "native-turn",
    "status": "in_progress",
    "items": [{
      "item_id": "native-item",
      "type": "agent_message",
      "phase": "commentary",
      "text": "Work in progress"
    }]
  }],
  "after_seq": 42,
  "replay_mode": "replace_observed_items"
}
```

Status comes from the native response; its names use snake_case. A native status object
remains an object. `turns` is omitted when the native response omits it, including when
`include_turns=false`. Native timestamps/name/workspace and item fields are optional.
Native turn IDs differ from the SDK `turn_id` returned by `messages.create`.
Errors are normalized and secret values are redacted; private reasoning is excluded.
Live reads include `owner={"turn_id": "sdk-turn", "native_turn_id": "native-turn"}`
from the verified owner connection, avoiding an event-persistence race when correlating
an admitted request. Idle reads omit `owner`. Ownership is identity, not execution status;
use the native thread/turn status to determine what the engine currently reports.

While a turn is alive, reads use that turn owner's connection. They never open another
execution. When no execution owner is alive, a temporary read-only native connection
reads the existing thread. Reopen resumes an unloaded thread without starting a turn.
Both methods retain the native identity and never resend previous input.

An idle read yields with the existing `busy` error while an unpaused, runnable queued
input awaits admission. It releases the instance lock without opening a temporary native
connection, so repeated observation cannot starve that input. Active-owner reads and
history reads for paused or held queues remain available. `busy` is not a native turn
status and never authorizes replay; keep confirmed history visible while observing again.

`after_seq` is immediately before the first saved event of the latest SDK turn. Follow
existing `instances.events` from that value. Native reads and notifications have no shared
cursor, so **never append replay deltas to snapshot text**. Rebuild each covered item from
its `item.started`, apply ordered deltas, then replace it with `item.completed` when present.
Use `(native_session_id, native_turn_id, item_id)` as identity. Merge the rebuilt items into
the snapshot, preserving snapshot-only items and fields. Apply replay catch-up coherently
before switching to live updates so old partial text does not replace newer visible text.
This also handles a delta emitted during the native read and a completion during reconnect.
In durable mode the response also contains the existing full `cursor`; pass that cursor
instead of `after_seq`. Neither cursor is a new execution authority.

The existing event envelope gains these native observations:

| Kind | Data |
| --- | --- |
| `thread.status` | `native_session_id`, `status` |
| `turn.started`, `turn.completed` | `native_session_id`, `native_turn_id`, `turn` |
| `item.started`, `item.completed` | Native IDs, `item_id`, `item` |
| `item.delta` | Native IDs, `item_id`, `field` (`text` or `output`), `delta` |
| `item.progress` | Native IDs, `item_id`, `message` |
| `turn.plan` | Native IDs, `plan`, optional `explanation` |

Each has `data.source="native"`. Fields are present only when the engine supplies them.
Live input echoes retain the existing safe notice; user input is available in authorized
history reads. Private subagent explanations are excluded, and unknown item payloads retain
explicit gaps rather than being copied into events.
Existing `run.*`, `message.*` and `tool.*` compatibility events remain available; consumers
choose one representation rather than displaying both. SDK queue delivery and process
recovery states must not be presented as native turn states.

`native_connection_unavailable` means the native operation could not be observed. It does
not mean the turn failed or stopped. Client timeout/detach leaves the execution running.
`native_owner_changed` rejects stale ownership. Callers may reconnect and read; neither
error authorizes replay. A live owner with no native control endpoint fails explicitly,
rather than presenting another process's disk snapshot as the live status.

## Native actions

Send through `messages.create(delivery="queue", idempotency_key=...)`; this uses the SDK's
single queue and native duplex transport even with `permission_mode="dontAsk"`. Steer
through the existing `delivery="steer"` and its `expected_turn_id` guard. Queued input is
SDK delivery evidence, not an invented native turn. Queue pause/reorder/remove remain
the existing `queues.*` operations. Explicitly resume a paused queue when appropriate.

`turns.interrupt(turn_id)` takes the **SDK turn ID** returned by message admission. It
pauses only that conversation's queue and sends native `turn/interrupt` through the owner
of that exact turn. The response is:

```json
{
  "instance_id": "sdk-instance", "turn_id": "sdk-turn",
  "native_session_id": "native-thread", "native_turn_id": "native-turn",
  "acknowledged": true
}
```

Acknowledgement is not termination: observe `turn.completed` or read native status.
Timeout does not prove interruption and never permits replay. Legacy `turns.stop` and
queue `delivery="interrupt"` remain process cancellation for compatibility and are
deprecated for native clients; they do not report a native interruption acknowledgement.

`turns.recover(turn_id)` is explicit process recovery, not reconnect. It pauses the affected
queue, requests stop through the existing run owner and, if needed, terminates only
identity-verified descendants of that execution. It returns `instance_id`, SDK `turn_id`,
`native_session_id` and `execution_stopped=true` only after the observed processes have
exited. It does not claim a native terminal result, reopen a thread or submit input.
`native_stop_unverified` leaves the queue paused and original evidence retained; the caller
must not rebind or replay. A newer turn makes old-turn recovery fail with `turn_conflict`.
Another conversation on the same account is outside the operation's scope.

`messages.lookup(instance_id, idempotency_key)` returns `{instance_id, found, message}`
from existing admission evidence, with `message=null` when absent. It never admits input.
An absent receipt cannot establish no prior external effect, especially after restore.
Existing duplicate keys retain their original instance, input and account binding.

## Missing instance metadata

`instances.reopen` optionally accepts `native_session_id`, `account_ref`, `workspace_path`
and `model`. With existing metadata these are identity/configuration assertions. If the
instance record is missing, all four are required with the **original SDK instance ID**:
the native home is scoped to that ID. The SDK validates the existing home and native
thread before rebuilding metadata. It does not scan other homes, select another account,
create a thread or bypass deleted-instance/native-binding guards. The bundled runtime's
exact `thread/resume` response confirming no rollout for the requested ID maps to
`native_thread_missing`. A generic invalid request, an unloaded thread, missing local
directory, configuration failure or lost connection cannot establish native absence.
When only the native-ID field is missing, an explicit original `native_session_id` can
restore it after native verification and the existing binding/continuity guards.

The deterministic subprocess test verifies partial text and tool output before completion,
native-only history, a delta during read, admission exclusion, client detach, scoped native
interrupt/recovery and reopening lost metadata without a second turn.
This is fixture evidence; live-provider acceptance is separate.
The actual bundled Codex also passes history/reopen checks against a deterministic
Responses server and emits native tool-start and interruption observations before turn
completion. Output deltas are forwarded when supplied; a background native command may
return a process-session handle without emitting incremental output notifications.
