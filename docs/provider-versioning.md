# Provider releases and shared contracts

AgentBridge indexes exact provider releases separately from its adapter dialects.
The bundled `provider_contract_index.json` is reviewed source data, shipped in
the wheel. Each immutable manifest has a SHA-256 content ID. Multiple release
bindings can reference that ID; a new CLI release does not require another
implementation when the reviewed contract still applies.

The initial bindings are Codex CLI 0.153.0 and Claude Code CLI 2.1.266.
The Claude entry describes a historical direct adapter. The active v2
diagnostic concerns the installed Codex worker that sends every turn through
CLIProxyAPI. A release binding does not certify an upstream model or OAuth flow.

## Runtime behavior and historical evidence

The direct-adapter checks below describe the earlier implementation. New v2
turns always use Codex through a verified proxy route. Native release checks
remain internal diagnostics of that Codex worker, not a user engine selector.

- A new native message checks the installed release before credential resolution
  and admission. The worker checks again before launching the provider.
- The admission receipt is persisted separately from the mutable release index.
  `turns.get` includes `provider_compatibility`; later upgrades do not rewrite it.
- Unknown, missing or inspection-confirmed incompatible releases block new native
  turns, live catalog/account refresh, reset redemption and AI diagnosis.
- Stop, cached observations, event following, export and reconciliation remain
  available. Completed idempotent results replay without invoking a new provider.
- Explicit custom launch commands are host-managed adapters. They return
  `custom_adapter` and `verification=unverified`; they are outside the release
  index and must be validated by their operator.
- An inconclusive inspection cannot clear an earlier drift finding. Matching
  evidence can clear drift for an already reviewed release. It never activates
  an unindexed release.

The runtime does not run a complete schema export before every message. Normal
admission checks the exact release plus stored decisive inspection results.
Inspect during installation or upgrades and whenever behavior diverges. It is
still possible for a backend or executable to change without changing its
version. Parsers continue to reject unknown terminal states and expose gaps.

## Inspection and visibility

```sh
agentbridge contracts list
agentbridge contracts check "Development Codex"
```

`contracts inspect` is an administrative drift diagnostic for a pinned
adapter release. Its legacy `--engine` argument names the installed adapter
to inspect; it does not select a conversation engine or bypass the proxy.

JSON-RPC exposes `contracts.list`, `contracts.get`, `contracts.check` and
`contracts.inspect`. The SDK exposes `provider_contracts`, `provider_contract`,
`provider_compatibility` and `provider_inspect`. Inspection creates local durable
evidence, without accounts, credentials, inference or automatic provider upgrades.

| Component | Offline evidence | Limit |
| --- | --- | --- |
| Codex CLI | Canonical generated JSON-schema bundle, default extraction flags | Method presence does not establish experimental eligibility; descriptions also affect the conservative hash |
| Claude CLI | Exact version and normalized `--help` observation | Help is not the control/stream protocol schema; equal help cannot certify equivalent behavior |

CLI version changes during extraction are drift. Output, files, JSON depth
and inspection time have explicit bounds. Schema generation's temporary disk
use is not an operating-system disk quota.

Inspection statuses are `unchanged`, `drift_detected`, `candidate` or
`insufficient_evidence`. An unknown release with matching structural evidence
can suggest an existing contract. Claude help observations never make that
suggestion. A suggestion is not approval or evidence of live acceptance.

## Indexing an upgrade

1. Inspect the installed candidate in an isolated environment and save its JSON.
2. Compare surfaces and run conformance fixtures for execution, metadata, unknown
   enums, interruption and controls. Repeat scoped live acceptance when required.
3. Choose the existing contract after review, or define a new manifest when the
   supported dialect changes. Existing immutable manifests remain available.
4. Generate a source-index proposal, review it and deliver the tested wheel:

```sh
python tools/index_provider_release.py --inspection candidate.json \
  --contract-id sha256:CONTRACT_ID --review-reference tests/provider-review
```

The tool prints a proposal by default. `--write` updates the checkout's source
index; it does not mutate a running registry or install the provider. `--manifest`
accepts a new manifest instead of an existing ID. Conflicting exact bindings are
rejected and require an explicit index migration review. A review reference is
traceability, not a signature or proof that the referenced checks were run.

## Boundaries that remain explicit

GrantBridge proxy login and CLIProxyAPI credential management use separate
private contracts and are not certified by these release bindings. Native
session files and Claude subscription quota remain compatibility surfaces,
not universal public APIs.

Disk failure can prevent durable evidence. A worker lost while its native child
is still alive remains unresolved rather than being rerun. Process-group cleanup
does not contain a deliberately escaped session. No software audit establishes
that all failures are impossible; unknown outcomes remain visible and require
inspection instead of an automatic retry.
