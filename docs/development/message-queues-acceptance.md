# Message queue acceptance

Checks on Linux x86_64, 2026-09-25. All execution checks use isolated synthetic
accounts and deterministic local provider subprocesses. They never discover or
use real account credentials.

## Contract and coverage

The [queue contract](../interface/message-queues.md) is implemented through the
SDK, CLI and JSON-RPC. Queue records and order are durable. A detached dispatcher
continues after client exit, and execution admission atomically checks the head
and links the original message ID to its turn.

| Behavior | Evidence |
| --- | --- |
| FIFO, insertion, move, deletion and optimistic concurrency | `tests/test_message_queues.py` |
| Concurrent duplicate requests admit one message and one turn | `tests/test_message_queues.py` |
| Native history resumes across queue turns and interruption | `tests/test_message_queues.py` |
| Steering acknowledgement, rejection and unknown delivery | `tests/test_message_queues.py` |
| Dispatcher loss, explicit restart and private context rebinding | `tests/test_queue_recovery.py` |
| Actual CLI client exit leaves accepted work running | `tests/test_queue_recovery.py` |
| Admission races, durable launch receipts and queue fences | `tests/test_queue_recovery.py` |
| Pending transcript entries and acknowledged live-input identity | `tests/test_queue_recovery.py` |
| Empty event read racing terminal commit | `tests/test_event_stream.py` |

A regression experiment sent a steering request and completed the turn without
acknowledging that request. The added test initially failed because the queue
remained runnable. The corrected terminal transaction preserves `unknown`,
pauses the queue with `unknown_outcome`, and never resends the input. Both this
case and provider disconnection before acknowledgement pass.

## Validation

| Check | Result |
| --- | --- |
| Repository naming and 450-line guard | Passed |
| `git diff --check` | Passed |
| Complete `python -m pytest -q` on an immutable source copy | 1042 passed, 3 skipped in 339.30 seconds |
| Queue, recovery, event-stream and duplex fault tests | 52 passed |
| Bundled x86_64 wheel build and bundle verification | Passed |
| Disposable wheel installation | Installed example, CLI help and queue execution checks passed |
| Repository `.venv` reinstallation | Installed example, CLI help and queue execution checks passed |

Installed execution checks exercise add, list, move, delete, pause, resume,
steer and interrupt through the installed SDK, imported outside the checkout.
They verify cancellation of the replaced turn and completion of queued work.

The complete suite used an immutable copy because other tasks were editing the
shared checkout. Its Python source matches both the delivered wheel and the
repository `.venv`. Two skipped tests require
`AGENTBRIDGE_CODEX_ACCEPTANCE_BIN`; the third expects a sibling GrantBridge
checkout, which is absent beside the temporary source copy.
The skipped GrantBridge fixture passed separately in the original checkout
(`test_grantbridge_proxy_adapter_starts_and_polls_a_local_fixture`: 1 passed).

The delivered wheel is
`ferran_agentbridge-2.3.3-py3-none-manylinux_2_28_x86_64.whl`, 179,760,345 bytes,
SHA-256 `ea5a8c943265ff35dad7506ab9dd266fced2cb850cf628f3f83804ce0d001042`.
Its Python source matches the validated source snapshot. Bundled versions are
Codex 0.153.0, CLIProxyAPI 7.3.16, Node v24.21.0 and GrantBridge
`d60873c999b7ee5215eb8cfa7bb65326175ff2f6`.

## Acceptance boundary

The pinned Codex executable's generated local schema confirms that steering
requires `threadId`, `expectedTurnId` and `input`, with `turnId` in the response.
This is an offline protocol check. Fixture success and schema agreement do not
establish live provider acceptance; live Codex, Claude and Grok queue/steering
acceptance remains pending. The package was not published and no Fullbrain
installation was modified.
