# Provider acceptance gate

AgentBridge separates implementation evidence from provider acceptance. The
local test suite proves the contract, persistence, redaction and worker
behavior with deterministic adapters. It does not authorize a claim that a
particular developer account or provider release works.

## Current matrix

| Provider | Contract fixtures | Real login | Fresh check | Real turn | Status |
|---|---|---|---|---|---|
| Codex | passing | passed | passed | passed | controlled live acceptance passed |
| Claude Code | passing | passed | passed with inference | passed | controlled live acceptance passed |
| Cursor | passing | passed | passed | passed | controlled live acceptance passed |

The matrix records controlled acceptance on 2026-09-20 with fresh authorized
accounts and the installed SDK. Each provider passed native continuation,
idempotent replay from a new client, bounded event reads and persisted Stop.
The private operator records retain account identities and execution evidence;
they are intentionally excluded from this repository.

Claude's final consent required direct control of the hosted browser, so a fully
unassisted mobile journey is not certified. Cursor cloud cancellation was not
independently observed outside the adapter. Fullbrain's actual integration was
not exercised, and the temporary acceptance host was shut down after the checks.

`capabilities.get` still defaults to `acceptance.provider_tested: false`: these
operator results do not certify another installation, version or feature set.
No provider is marked usable solely because a browser URL exists. See
implementation-status.md for unimplemented features and deployment requirements.

## Release gate

Before enabling a provider in a production Fullbrain deployment, run the
deterministic suite and a disposable provider acceptance job for that provider.
The job must record the provider and runtime versions, the isolated data
directories, the account identity, the fresh-process check, one authenticated
operation, a restart and reconnect, and the exact operations not tested. It must
also verify that credentials are absent from argv, SQLite, logs, prompts and
events.

The job must use a disposable account and explicit credential authorization. It
must not discover or reuse a developer's default account, and its result must
not be copied into the capability payload unless the recorded provider version
and checks match the deployment. A failed or incomplete provider job leaves the
operation at `fixture_tested` or `unsupported`.

## Local verification

```bash
python -m pytest -q
python tools/check_repository.py
python -m compileall -q src
python -m pip wheel . --no-deps --wheel-dir /tmp/agentbridge-wheel
```

The wheel and installed CLI checks are part of the release procedure. Live
provider checks remain an operator-controlled step because they require access
to disposable accounts and credentials.
