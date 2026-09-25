# Playground page and server compatibility

Date: 2026-09-25.

## Observed failure

A running playground process had loaded the HTTP handler before account routing
and persisted access controls were added. Its static file handler read the new
JavaScript from disk. Sending from that page therefore supplied fields the
running handler did not accept, yielding `invalid_request` before SDK admission.
Browser tests passed because their fixtures started a fresh server every time.
They did not demonstrate that an already-running installation had been updated.

The affected server started at 10:42; the relevant source changed at 12:03.
Nonexistent-instance probes reproduced the difference without creating a chat
or invoking a provider: `model` reached SDK lookup, while `routing_mode`,
`permission_mode` and `sandbox_mode` failed HTTP field validation.

## Correction

The local service was restarted with the updated installed SDK after checking
there were no active turns or pending deliveries. Existing conversation IDs,
native Codex bindings and turn records were unchanged across the final restart.
A Playwright probe against that actual service loaded its served API module
and confirmed the routing/access fields now reach SDK lookup. It used an
intentionally nonexistent instance, without discovering or using an account.

The playground now advertises `api_revision` in `/api/meta`. Before chat
mutations, the browser verifies that revision; the handler also checks the
request header to handle a server update between verification and admission.
Incompatibility returns `playground_update_required` with restart/reload
instructions. The draft remains and no automatic retry is made. Stop and
permission responses remain available without the chat revision header.

## Regression evidence

- HTTP contract and existing server tests: 28 passed.
- New Playwright revision regressions: 7 passed. Missing/different revisions
  are exercised at create, update and message submission, plus a server-change
  race after verification. Explicit retries complete once with the same native
  session and retained idempotency payload where applicable.
- Existing access, route reconfiguration and control browser tests: 13 passed.
- Final complete suite: **1,113 passed in 454.78 seconds**, with no failures or
  skips. The optional native startup checks used the reviewed bundled Codex.
- Repository checks and `git diff --check` passed. Wheel construction,
  disposable installation, installed quickstart and CLI help also passed.

These automated cases use synthetic accounts and local provider fixtures.
They do not establish external provider acceptance. An SDK package version
alone is not proof that a running playground process has loaded that version.
