# Selected context and private execution tools

`messages.create` accepts optional `context_package` and `mcp` objects. This
adapter implementation is covered by deterministic fixtures; acceptance with
a provider and the host sandbox is pending. An isolated local Codex 0.153.0
app-server accepted `thread/start` with a required MCP server and completed
its `tools/list` handshake against a fake Unix facade; no account or provider
turn was used in that check.
The host still owns selection, authorization, operation-bound tool grants and
revocation. AgentBridge validates and transports that selection.

## Context package

Version 2 requires `version`, `selection_hash`, `instructions`, `exclusions`,
`evidence` and `tools`. Version 1 omits `tools` for older callers. An optional
`execution_mode` is `normal`, `inputs_only` or `evaluation_inputs_only`. Each
instruction has `instance_id`, positive `revision`, `kind` (`rule` or `skill`),
`scope` and content-addressed `assets` with relative `path`, `content` and
SHA-256 `digest`. Every skill includes `SKILL.md` with a `name` frontmatter
field. Evidence has `source_ref`, `kind`, `text`, SHA-256 `digest`, `freshness`
(`fresh`), `authority` and `channel` (`evidence`). A v2 package with selected
tools requires an `mcp` descriptor.

The `selection_hash` is SHA-256 over canonical JSON with sorted keys, compact
separators and UTF-8 encoding. Its input has `context_refs` in evidence order,
instructions with asset `path` and `digest` only, `exclusions`, and `tools`
for version 2. The package is limited to 512 KiB, instruction content to
256 KiB and evidence text to 64 KiB, with further item-count bounds.
Inputs-only modes reject both selected tools and an MCP descriptor.

AgentBridge sends rules as Codex developer instructions, selected skills as
native skill inputs and evidence as explicitly labelled untrusted user data.
It creates selected skill files in a private temporary directory and removes
them after the turn; a later turn clears orphaned temporary directories before
loading its selection. Before starting a Codex thread it requests native
`skills/list` for the exact workspace, verifies every selected skill is present,
and explicitly disables every discovered unselected skill. A malformed list,
wrong workspace or missing selected skill fails before `thread/start`.
Selected-context configuration also disables project instruction loading,
web search, apps, subagents and built-in shell execution. The selected workspace
is intended to be accessed through separately admitted MCP tools;
evaluation mode additionally requests an ephemeral thread and refuses native
resume. A native configuration rejection fails the turn before execution.
Use `instances.create(evaluation: true)` for disposable evaluations. The
instance then requires `evaluation_inputs_only`, accepts one turn, and must be
finished with `instances.discard_evaluation` after its result has been saved by
the caller. A pending discard does not imply that the turn or local processes
have stopped. The discard removes local evaluation evidence and the private
Codex home; it does not alter proxy accounts or upstream credentials.
The context content is delivered over private process
pipes and is not stored in the AgentBridge database. A SHA-256 digest is saved
with the turn for idempotency; changing context while reusing an idempotency
key produces `idempotency_conflict`. A stopped or interrupted turn with pinned
context must be continued through a new `messages.create` with fresh context.

## MCP descriptor

`mcp` version 1 contains only `version`, absolute `socket_path`, canonical
UUID `operation_id` and opaque `capability`. The descriptor is delivered over
the private worker pipe. The capability goes to the Codex child through a
process environment variable, while Codex's MCP configuration names that
variable without embedding its value. AgentBridge starts a private stdio MCP
server that forwards `tools/list` and `tools/call` to the Unix socket using one
channel ID and fresh call IDs. The host socket must enforce operation, channel,
grant and revocation checks. AgentBridge returns a generic error for a failed
socket call and does not persist the capability or raw facade errors.

Codex is also asked to disable its built-in shell execution tools when MCP is
configured without a package. The host must still test
its actual Codex version and sandbox; this configuration is not a substitute
for host isolation. Neither the MCP descriptor nor context package authorizes
an automatic rerun or account switch during a turn.
