# Interface rationale

AgentBridge exposes one vocabulary for every provider. The embedding
application chooses a model, permissions and product retry policy. For v2
proxy instances AgentBridge chooses an eligible account before each turn,
translates the request and records the selected route and its evidence.
Historical direct instances remain readable but cannot execute new turns.

## Discovery

capabilities.get is the first call because a caller must know whether a
parameter is native, translated, fallback or unsupported before creating work.
models.list is separate from capabilities because a catalog changes over time
and a model turn must never be spent just to discover available models.

accounts.list is a reference lookup. accounts.status reports identity and
authentication observations. accounts.login.start, status, check, complete and
cancel are asynchronous because browser and device flows can outlive one
process. GrantBridge coordinates provider OAuth through a dedicated local
CLIProxyAPI Management API; the proxy owns upstream credentials. A v2 caller
may pin an account explicitly; otherwise AgentBridge
selects from accounts declaring the chosen model. A route is fixed during one
turn, and a later change is visible in turn records and events. The same
management key reference is required for pinned and automatic routes.

## Usage

usage.get returns an observation for one scope. Account quota is kept separate
from turn token usage because a quota window and a request total have different
freshness and accounting rules. usage.history is append-only evidence, so a
caller can explain stale or changing values without treating missing data as
zero.

## Instances

instances.create validates account, workspace and defaults without invoking a
model. instances.get and instances.list make the durable conversation visible.
instances.update uses expected_version to prevent lost writes. instances.archive
is reversible at the storage layer and refuses an active turn. instances.export
produces a bounded portable evidence bundle. instances.transfer reports native,
portable or unavailable continuation and leaves the source independent.

## Messages and turns

messages.create is the normal asynchronous admission point. It accepts text
blocks, common execution controls, a request key and bounded budgets. The
request key makes duplicate delivery safe. messages.list reads the transcript
without requiring a provider call.

turns.list and turns.get expose the accepted execution attempt. turns.events and
instances.events use sequence offsets so a disconnected client can resume
observation. turns.stop targets one exact turn. turns.resume is always
explicit and includes uncertainty context when the previous provider outcome
was not observed. recover marks a lost worker interrupted and never retries it.

permissions.respond is reserved for adapters that can pause and resume a
provider request. It records the host decision only after matching a pending
request. Codex delivers one-request decisions through its supervised duplex
protocol. Historical direct Claude behavior is not a v2 execution path.

## Parameters and unsupported behavior

model, effort, workspace_path, timeout_ms, permission_mode, sandbox_mode,
allowed_tools, max_turns and max_budget are common parameters because they
describe execution intent. They are not all usable in the v2 adapter:
`allowed_tools`, `max_budget` and advanced instance defaults are unsupported.
The adapter accepts a positive numeric `context_window` per turn; a host should
expose it only when a model maximum has been observed. Provider acceptance
remains pending. Attachments
accept bounded inline text and images, with explicit omissions on portable
transfer. Provider-only switches belong under provider_options and remain
unsupported without a reviewed contract.

Every operation accepts only the parameters documented for that method.
Unsupported values fail before a provider request and return a stable error
with an action hint. A fallback catalog is marked stale. A provider failure
does not become an empty list, a successful turn or a retry.

## Response and error invariants

Resources expose durable identifiers and observation timestamps. Implemented
collection envelopes vary: `models.list` includes `items`, `next_cursor` and
`has_more`, while accounts, instances, events and usage history return arrays
with method-specific limits. Events retain their
sequence, instance, turn, engine and normalized kind.

Errors contain a stable code, category, phase, outcome, retryability and
safe details. Credentials, raw provider payloads and private model reasoning
never appear in errors or persisted evidence.
