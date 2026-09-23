# AgentBridge interface

This folder contains the provider-neutral design and its implemented subset.
[implementation-status.md](implementation-status.md) is the implementation inventory;
[../protocol.md](../protocol.md) documents compatibility. Design coverage is not
implementation evidence. Fixture tests and live-provider acceptance are separate.

## Resources

- resources.md: account, model, instance, message and turn records.
- operations.md: method names, parameters and return values.
- capabilities.md: v2 proxy behavior, support and acceptance maturity.
- events.md: event envelope and stream semantics.
- interactive-inputs.md: implemented approvals, attachments, catalogues and quota limits.
- errors.md: stable error envelope and retry behavior.
- review.md: completeness checklist and acceptance evidence.
- provider-acceptance.md: release matrix and live-provider gate.
- v2-model-routing.md: model-first Codex proxy routing and evidence limits.
- rationale.md: why each operation exists and how unsupported parameters behave.

## Contract rules

AgentBridge is an execution and observation layer. The host chooses the model,
authorizes tools and external side effects, and owns product retry policy.
For automatic proxy instances, AgentBridge selects an account before each
turn and persists the route and its evidence. Pinned proxy instances keep an
explicit account. Historical direct instances remain readable but cannot
execute. AgentBridge never silently retries an
unknown provider outcome or changes accounts during a turn.

The AgentBridge contract is the only public contract. Provider protocols,
commands, SDK objects and native field names stay inside adapters. A capability
may be implemented with a native provider call, an adapter translation or a
fallback, but callers always use the same AgentBridge method, parameter and
error vocabulary. Provider-specific behavior is exposed only through a
validated provider extension when the common contract cannot express it.

The public model has four durable identifiers:

    account_ref  human-facing unique account name
    instance_id  durable conversation
    message_id   accepted user message
    turn_id      one provider execution for that message

Internal UUIDs may be stored, but responses use the stable public reference when
one exists. A message_id is not a turn_id: one message may be recovered or
continued through more than one execution attempt in the target design. The current
resume implementation creates a new input/message and turn; shared message identity
across retries is not implemented.

The JSON-RPC transport remains stdin/stdout. No network listener is required.
Methods are asynchronous by default and return a durable identifier quickly.
Clients poll `instances.events` or `turns.events`; the first Fullbrain adapter
does not use a blocking conversation follow.
