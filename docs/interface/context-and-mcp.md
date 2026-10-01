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
for version 2. The canonical package is limited to 1.5 MiB (1,572,864 bytes),
instruction asset content to 1 MiB (1,048,576 bytes) and evidence text to
64 KiB, all counted as UTF-8 bytes, with further item-count bounds. A host can
therefore deliver a full 1 MiB instruction catalogue selection in one turn.
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
web search, apps and subagents. Built-in shell execution is disabled unless the
explicit [host-isolated full-access profile](execution-access.md) is selected. The selected workspace
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

## Delivery by upstream provider

Codex is the only execution engine (see [model routing](v2-model-routing.md)).
An account's `provider` names the upstream OAuth credential behind its
dedicated CLIProxyAPI sidecar; it does not select a second engine. The context
package therefore takes one path for every provider: a turn with selected
context or an MCP descriptor runs the Codex app-server through the sidecar's
Responses endpoint, with rules as developer instructions, skills as native
skill inputs, evidence as labelled untrusted input and the same model, effort
and `context_window` overrides. There is no Claude Code or Grok CLI path, no
`--append-system-prompt` file and no `.claude` directory; nothing from the
package is written outside the private per-instance Codex home.

| Upstream provider | Rules and skills | Implementation and fixtures | Live acceptance |
| --- | --- | --- | --- |
| `codex` | Developer instructions and native skill inputs | Deterministic app-server fixture: `tests/test_interactive_inputs.py` | Pending |
| `claude` | Same Codex app-server path through the sidecar | `tests/test_context_upstream_providers.py` with a `claude` sidecar credential | Pending: the sidecar must translate developer instructions, skill inputs and effort to the Anthropic API |
| `grok` | Same Codex app-server path through the sidecar | `tests/test_context_upstream_providers.py` with a `grok` sidecar credential | Pending: the sidecar must translate developer instructions, skill inputs and effort to the xAI API |

The fixtures prove what AgentBridge hands to the Codex app-server and that the
route, model, effort and context-window overrides apply for each provider.
They do not prove that CLIProxyAPI or the upstream model honours developer
instructions, skill files or reasoning effort; each provider and model needs
its own live acceptance record before that capability is claimed. An account
that is not bound to a local proxy fails with `invalid_proxy_account` before
any native process starts, whichever engine or provider it names.

## MCP descriptor

Version 2 accepts `version`, canonical UUID `operation_id`, opaque `capability`,
and `servers`: 1–100 unique `{name, url}` entries. URLs are bounded HTTP endpoints
on `127.0.0.1` with an explicit port, no userinfo, query or fragment. The host owns
that isolated loopback listener and enforces execution scope and revocation.
This version requires `host_isolated: true`. AgentBridge configures those native
MCP servers on thread start/resume, with a bearer header and tool approval mode
`approve`; it does not create a second MCP bridge. Startup failure of any selected
server fails execution. A new turn can select different connections while keeping
the native thread. Rebinding the same turn can rotate capability, but cannot change
the admitted endpoints or execution package. Only digests enter persisted options;
the capability remains private worker input and is redacted from observations.

Context package version 2 optionally accepts `read_only_paths`, up to 16 unique
canonical absolute directory paths. They are host-selected projections, independent
of MCP. The full execution package digest binds them for replay and queue recovery.
Inputs-only packages reject them. Host-isolated execution mounts each directory read
only in the existing native namespace; paths overlapping cwd, native state, temporary
storage or system mounts are rejected. The host must prepare authorized projections
and hold admission closed while replacing them. Native checkpoints do not capture cwd.
The same package may set `workspace_write` explicitly to `false` or `true` in
host-isolated execution. `false` mounts cwd read only even with Codex full access;
`true` allows local cwd writes. Omission preserves the historical host-isolated
default. This bound host policy cannot be changed when rebinding the same turn.
Deterministic fixtures cover configuration and scope rejection; deployed Codex and
host filesystem acceptance remains the integrator's required check.

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
configured without a package, except with explicit `host_isolated: true`. The host must still test
its actual Codex version and sandbox; this configuration is not a substitute
for host isolation. Neither the MCP descriptor nor context package authorizes
an automatic rerun or account switch during a turn.
