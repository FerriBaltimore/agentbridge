# Reviewed error learning

AgentBridge captures unfamiliar provider failures as durable local cases. A
reviewed rule can improve classification of later matching failures. It cannot
change execution policy, retry work or edit application code.

## Safe evidence

Cases retain the provider label and exact observed version, occurrence count,
first and last observation, and turn references when available. Error evidence
is limited to hashes, a fixed vocabulary of signals, recognized JSON field
shapes and an HTTP error status when reported.

Original error bodies, arbitrary native code strings, private reasoning,
credentials and free-form explanations are not stored in cases or proposals.
The raw input is inspected only in memory through a bounded field traversal.
The stored fingerprint is a hash, not a retained copy of the error message.

Safe signals can be too weak to establish a cause. Reviewers should leave such
cases unclassified. An unknown provider feature or new category requires a
normal implementation change; a learned rule does not extend the accepted
categories or add executable parsers.

## Inspect cases and historical diagnoses

All successful `errors` commands return JSON. Service errors return JSON on
stderr with a nonzero exit status. Command help and argument validation use
normal CLI output.

```sh
agentbridge errors cases --limit 20
agentbridge errors cases --limit 20 --cursor 20
agentbridge errors case CASE_ID
agentbridge errors diagnosis PREVIOUS_REQUEST_KEY
```

AI diagnosis has no verified Codex-to-proxy implementation in v2.
`agentbridge errors diagnose` and `Bridge.error_diagnose` return
`unsupported_operation` before selecting an account, calling a provider or
creating an operation. There is no direct provider diagnostic path.

`errors diagnosis` reads a previously saved diagnostic receipt without
restarting inference. An interrupted historical operation can remain
`submitted` with an uncertain outcome. If its proposal was saved before an
interruption, reading the receipt can reconcile it as `completed`. A `failed`
receipt remains failed; it is not treated as an insufficient-evidence judgment.

## Validate, review and activate

A manually created proposal or a completed historical diagnostic receipt can
be inspected and validated:

```sh
agentbridge errors proposal PROPOSAL_ID
agentbridge errors validate PROPOSAL_ID
```

Validation checks matching boundaries, preservation of known classifications,
unknown outcomes and the prohibition on automatic retries. Its result states
`kind: structural_fixture` and `semantic_verification: false`. Passing these
checks does not establish that the proposed cause is correct. Review the case
and proposed conclusion before activating the rule.

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

A rule matches the exact provider label, version and evidence fingerprint. An
unknown version matches only another unknown version; it is not a wildcard.
Rules classify only failures that the built-in adapter could not recognize.
They cannot override a recognized safety block, account quota, authentication
failure or another known classification.

Learned classifications retain the original execution phase and outcome,
including unknown effects. They always keep `retryable: false` and
`action: inspect`, with the rule ID and revision attached to the resulting
error. Historical event records are not rewritten by activation.

## Python API

The CLI uses `Bridge.error_cases`, `error_case`, `error_diagnosis`,
`error_proposal`, `error_validate`, `error_activate`, `error_deactivate` and
`error_rule`. `Bridge.error_diagnose` exists but returns
`unsupported_operation` in v2. Trusted local callers can create a manual
candidate through `error_propose(case_id, result)` with exactly:

```json
{"status":"proposed","target_code":"provider_unavailable"}
```

Only existing canonical target codes are accepted. Capturing a case, inspecting
it or validating a proposal never calls a model. AgentBridge does not provide
a background diagnosis scheduler or an automatic rule-activation policy.
