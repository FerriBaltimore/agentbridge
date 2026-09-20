# Reviewed error learning

AgentBridge captures unfamiliar provider failures as durable local cases. A
separate, explicitly requested AI session can suggest an existing normalized
error category. A reviewed rule improves classification of later matching
failures. It cannot change execution policy, retry work or edit application
code.

## Safe evidence

Cases retain the provider engine and exact observed version, occurrence count,
first/last observation and turn references when available. Error evidence is
limited to hashes, a fixed vocabulary of signals, recognized JSON field shapes
and an HTTP error status when reported.

Original error bodies, arbitrary native code strings, private reasoning,
credentials and free-form AI explanations are not stored in cases or proposals.
The raw input is inspected only in memory through a bounded field traversal.
The stored fingerprint is a hash, not a retained copy of the error message.

Safe signals can be too weak to establish a cause. Such a diagnosis must return
`insufficient_evidence`. An unknown provider feature or new category still
requires a normal implementation change; the AI does not invent executable
parsers or extend the accepted categories automatically.

## Inspect and diagnose

All successful `errors` commands return JSON. Service errors return JSON on
stderr with a nonzero exit status. Command help and argument validation use
normal CLI output.

```sh
agentbridge errors cases --limit 20
agentbridge errors cases --limit 20 --cursor 20
agentbridge errors case CASE_ID
agentbridge errors diagnose CASE_ID --account-ref "Diagnostic Cursor" \
  --model MODEL_ID --idempotency-key diagnosis-request-001 --timeout 60
agentbridge errors diagnosis diagnosis-request-001
```

The diagnosis currently requires an explicitly selected, bound Cursor account
and model. That account can investigate cases from Codex, Claude or Cursor.
The diagnostic backend choice does not change the original failed session,
account or model. It starts a separate model request and may consume usage.

The provider worker receives only validated safe evidence and allowed target
codes. It uses a disposable empty home, workspace and native state directory,
without prior conversations, local settings, tools, MCP servers or subagents.
Credential references are resolved for the explicitly selected account. The
supervisor destroys the temporary state and provider process group on exit.
Native SDK scratch state uses a verified writable `tmpfs` at `/dev/shm`,
including its temporary home and workspace. Diagnosis fails closed on hosts
without that volatile filesystem instead of writing native transcripts to disk.
The current isolated diagnostic backend therefore requires Linux with tmpfs.

The diagnostic subprocess elapsed-time bound is 1-120 seconds, default 60.
Credential resolution precedes that bound. Both the worker
response and supervisor output are limited to 8 KiB. These bounds are not token
or monetary budgets. No automatic account or model fallback is performed, and
the diagnostic bridge disables its connection retries.

The operation is persisted before inference. Reusing its idempotency key reads
the saved operation instead of launching another session. Conflicting inputs
are rejected. An interrupted operation can remain `submitted` with an uncertain
outcome; reading it does not silently repeat inference. A proposal saved before
an interruption can be reconciled into its receipt.
Technical failures carry `state: failed` and bounded reason/phase metadata;
they are not reported as successful AI judgments of insufficient evidence.

## Validate, review and activate

A completed diagnostic receipt references a proposal. It does not activate it.
Inspect that proposal and its case, then validate the proposed classification:

```sh
agentbridge errors proposal PROPOSAL_ID
agentbridge errors validate PROPOSAL_ID
```

Validation checks matching boundaries, preservation of known classifications,
unknown outcomes and the prohibition on automatic retries. Its result states
`kind: structural_fixture` and `semantic_verification: false`. Passing these
checks does not prove the AI correctly understood the provider error. Review
that conclusion before activating the rule.

```sh
agentbridge errors activate PROPOSAL_ID --expected-revision 2
agentbridge errors rule RULE_ID
agentbridge errors deactivate RULE_ID --expected-revision 1
```

Use the actual revision returned by the preceding read or validation. The
numbers above illustrate a newly validated proposal and a newly activated
rule. A stale revision fails instead of changing a record reviewed earlier.
Activation and deactivation are explicit local mutations, recorded in a durable
audit. Deactivation preserves the case, proposal and rule history.

A rule matches the exact engine, provider version and evidence fingerprint.
An unknown version matches only another unknown version; it is not a wildcard.
Rules classify only failures that the built-in adapter could not recognize.
They cannot override a recognized safety block, account quota, authentication
failure or another known native classification.

Learned classifications retain the original execution phase and outcome,
including unknown effects. They always keep `retryable: false` and
`action: inspect`, with the rule ID and revision attached to the resulting
error. Historical event records are not rewritten by activation.

## Python API

The CLI uses `Bridge.error_cases`, `error_case`, `error_diagnose`,
`error_diagnosis`, `error_proposal`, `error_validate`, `error_activate`,
`error_deactivate` and `error_rule`. Trusted local callers can also create a
manual candidate through `error_propose(case_id, result)` with exactly:

```json
{"status":"proposed","target_code":"provider_unavailable"}
```

Only existing canonical target codes are accepted. Capturing a case, inspecting
it or validating a proposal never calls a model. AgentBridge does not provide
a background diagnosis scheduler or an automatic rule-activation policy.
