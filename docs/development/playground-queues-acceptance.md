# Playground message queue acceptance

Date: 2026-09-25. Linux x86_64; browser checks use Playwright with Chrome.

## Behavior

Chat submits messages with `delivery="queue"`, including the first turn, so
subsequent input can be queued or steered. The composer remains usable while
a turn runs. The Queue panel lists pending messages in SDK order and provides
move up/down, remove, pause/resume, Send now and Stop & send controls.
The composer also offers queue, active-turn input and interruption.

The HTTP adapter calls only public SDK methods. The browser never schedules
turns: it observes the SDK's queue and current turn, reconnects event streams
as queued turns start, and restores queue state after reload. Pending messages
appear in the queue rather than as already delivered transcript entries.
Unconfirmed and rejected live inputs retain explicit delivery labels.

Edits send the observed queue version; immediate delivery also sends the
observed turn ID. Conflicts refresh the view without repeating a mutation.
An explicit retry after a lost steering receipt retains both its request key
and original target turn, even after that turn finishes. It reconciles the
original message instead of creating a new queued execution.

HTTP API revision 2 prevents pages using queue controls from submitting to
the previous server contract. A page/server mismatch preserves the draft.

## Recorded checks

| Check | Result |
| --- | --- |
| `python -m pytest -q --ignore-glob='tests/test_playground_browser*.py'` | 1057 passed, 2 skipped in 160.18 seconds |
| Browser suite plus playground HTTP/server/SSE tests | 113 passed in 336.31 seconds |
| Final focused browser queue tests | 5 passed in 28.64 seconds |
| Repository guard, JavaScript syntax and `git diff --check` | Passed |
| Wheel build and bundle verification | Passed |
| Disposable wheel installation and repository `.venv` | Installed example, CLI and queue execution smoke passed |

The suites overlap on HTTP/server tests; their counts are not additive.
The two skipped native sandbox tests require `AGENTBRIDGE_CODEX_ACCEPTANCE_BIN`.
All execution tests use synthetic local accounts and provider subprocesses.

The five queue browser cases cover durable order across reload, removal,
pause/resume, queued steering, composer steering/interruption, competing edits,
and lost-receipt reconciliation after completion. Layout checks include
1440, 390 and 320-pixel widths. A regression initially put the Send button
below the 700-pixel viewport with three pending messages. Bounding and shrinking
the queue panel, with internal scrolling, keeps the composer visible.

The verified x86_64 wheel is 179,765,835 bytes, SHA-256
`37adc1010810cf106bc10591cfef372a9f214dcceaf7ed751fcd704463cdf3c8`.
Its Python source and the repository `.venv` match the checkout. The playground
remains a repository client and is not part of the runtime wheel.

## Running local service

The service at `http://127.0.0.1:8765/` still advertised revision 1 before the
update. There were no active turns or pending deliveries. It was restarted
with the updated installed SDK and now advertises revision 2. Its one saved
conversation, native conversation identity and six recorded turns were unchanged.
The before/after state fingerprint was
`564d3053ea9b9fc6975106d80ebea8436149cc03a0a681c7cb554d08d539d718`.

A read-only browser probe verified the served Queue panel, pause control and
composer without JavaScript errors. It did not submit provider work. Live
upstream acceptance of steering remains separate from fixture and local UI
acceptance. No Fullbrain installation was changed and no package was published.
