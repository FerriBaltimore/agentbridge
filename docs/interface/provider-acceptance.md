# Provider acceptance gate

AgentBridge separates implementation evidence from provider acceptance. The
local test suite proves the contract, persistence, redaction and worker
behavior with deterministic adapters. It does not authorize a claim that a
particular developer account or provider release works.

## Historical direct-provider matrix

| Provider | Contract fixtures | Real login | Fresh check | Real turn | Status |
|---|---|---|---|---|---|
| Codex | passing | passed | passed | passed | controlled live acceptance passed |
| Claude Code | passing | passed | passed with inference | passed | controlled live acceptance passed |

The matrix records controlled acceptance on 2026-09-20 with fresh authorized
accounts and the installed SDK. Each provider passed native continuation,
idempotent replay from a new client, bounded event reads and persisted Stop.
These direct paths are read-only historical records in v2 and cannot execute
new turns. No cell in this matrix certifies the Codex-through-CLIProxyAPI path.
The private operator records retain account identities and execution evidence;
they are intentionally excluded from this repository.

Claude's final consent required direct control of the hosted browser, so a fully
unassisted mobile journey is not certified. Fullbrain's actual integration was
not exercised, and the temporary acceptance host was shut down after the checks.

`capabilities.get` reports `runtime_provider_support_verified: false` and
fixture maturity for the v2 route: these operator results do not certify
another installation, version or feature set.
No provider is marked usable solely because a browser URL exists. See
implementation-status.md for unimplemented features and deployment requirements.

## V2 release gate

V2 has deterministic fixture evidence for the local proxy route and the
GrantBridge-to-Management-API login flow. Controlled live OAuth, model
entitlement, provider-specific tools, quota and account changes are pending.
The initial login browser runs on the same host as the sidecar.

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

Build a platform wheel for the Linux architecture under test. On a source
build, missing upstream archives are downloaded and matched to the committed
SHA-256 lock. The installed wheel carries those archives and resolves the
runtime offline. Verify both architectures before distributing both wheels;
a local build on one machine only checks that architecture.

```bash
python -m pytest -q
python tools/check_repository.py
python -m compileall -q src
python -m pip wheel . --no-deps --wheel-dir /tmp/agentbridge-wheel
```

The wheel and installed CLI checks are part of the release procedure. Record
the wheel tag, pinned component versions and binary hashes. A new wheel does
not restart existing sidecars: explicitly drain and restart them, then check
their actual runtime version. Live provider checks remain an operator-controlled
step because they require access
to disposable accounts and credentials.

## Input and observation acceptance (2026-09-20)

Tested versions: Codex CLI 0.153.0 and Claude Code 2.1.266. These checks used
explicitly authorized accounts and isolated workspaces through AgentBridge's
public methods:

- Both providers correctly identified a generated red image passed as an
  inline attachment. No filesystem image path or public upload was supplied.
- Codex and Claude resumed those sessions through the new duplex transport and
  recalled the previous image, preserving the native session identity.
- For each of Codex and Claude, one requested shell write was allowed and created
  the expected isolated marker. A separate denied write did not create its file.
  Decisions matched the exact expected command; no broad grant was issued.
- Stop was exercised while each native provider awaited a permission. Both runs
  became cancelled and no marker was created. A follow-up process check found
  no remaining native group. A transient Claude child observed immediately after
  cancellation prompted an additional worker cleanup guard; deterministic tests
  now cover a native child that deliberately ignores SIGTERM.
- Claude's initialize catalogue returned five models without inference. Its
  bound OAuth profile returned three account utilization windows. Counts are dated
  observations, not fixed catalogue assertions or entitlement guarantees.

The initial Claude Stop prompt did not produce a tool request and is not counted
as a pending-permission test. A second explicit request did, and was cancelled.
Claude quota uses native OAuth compatibility. PDFs,
other binary attachment types and complete per-model metadata are not certified.
The capability defaults remain fixture_tested until an embedding deployment
records matching evidence. See interactive-inputs.md for the exact contract.
