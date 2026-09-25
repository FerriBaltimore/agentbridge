# Execution access

Every chat keeps one native Codex session. Access settings, model and upstream
account are separate configuration; changing them never replaces that session.
`capabilities.get.parameters` declares supported values and their limitations.

## Persistent defaults and overrides

`instances.create` and `instances.update` accept `sandbox_mode` and
`permission_mode`. `instances.get` and `instances.list` return the saved values.
New and migrated instances default to `read-only` and `dontAsk`.
Updates are atomic, support `expected_version`, and require an idle instance
with no pending queue work. Changing defaults does not rewrite earlier turns.

`messages.create` inherits omitted settings from the instance. An explicit
value overrides that setting for the new turn only. Accepted turns and queued
messages retain their admitted values. Steering with omitted settings inherits
the active turn's settings; it cannot change that turn's access policy.
An idempotent replay keeps the original accepted settings even if defaults have
since changed. `run.started` includes `sandbox_mode` and `permission_mode`.

```python
chat = bridge.instance_create(
    model="YOUR_MODEL", workspace_path="/path/to/workspace",
    sandbox_mode="workspace-write", permission_mode="default",
)
bridge.instance_update(chat["id"], sandbox_mode="danger-full-access",
                       permission_mode="dontAsk", expected_version=chat["version"])
bridge.message_create(chat["id"], "Perform the requested work")
```

The same fields are available through JSON-RPC and CLI instance commands using
`--sandbox-mode` and `--permission-mode`. The playground saves both per chat.

## Filesystem and native tools

| `sandbox_mode` | Effective execution |
| --- | --- |
| `read-only` | Workspace reads; the outer filesystem boundary blocks workspace writes and unrelated host paths. Native session and private temporary state remain writable. |
| `workspace-write` | Workspace reads and writes, with the same outer boundary. Codex applies its native workspace policy to commands. |
| `danger-full-access` | Explicit trusted execution using host filesystem and network permissions, including paths outside the workspace. AgentBridge does not apply its outer filesystem sandbox. |

Full access can reach any file available to the service user, including private
application state. It is not a tenant isolation boundary. Deploy hosts needing
separation under distinct service identities or external containers. The native
process inspection syscall filter remains enabled as defense in depth.
Neither mode grants rights denied by the operating system or host container.
The private, per-instance `CODEX_HOME` remains the same in every mode.

Restricted native commands require Linux user, mount and PID namespaces. Their
outer filesystem exposes only the workspace, native state, private temporary
directory, selected system dependencies and reviewed runtime. Its private
procfs contains native processes rather than host workers. Codex's nested
command sandbox remains responsible for network and protected metadata rules.
Private AgentBridge state must stay outside exposed system runtime directories
such as `/usr`; admission rejects that placement with `unsafe_store`.
Selected context disables native shell tools and retains the Landlock boundary,
including on hosts which disable nested user namespaces. An approval
does not remove that outer boundary. Native Codex controls command network
access inside restricted modes. Upstream model traffic still uses the local
proxy. Web search is a separate native tool whose availability also depends on
the upstream provider and model; full access does not establish that support.

## Approval policy and selected context

`dontAsk` maps to Codex `never`: commands allowed by the selected sandbox can
execute, and requests requiring approval cannot be approved through the host.
`default` maps to Codex `on-request`: Codex may request an individual host
decision through the [permission protocol](interactive-inputs.md). It does not
mean every action requires approval, nor grant access to every host file.

Selected `context_package` and private `mcp` inputs use their explicit
[isolation contract](context-and-mcp.md). Combining them with full access raises
`invalid_execution_policy` before admission. The SDK never silently grants
full host access to such a request or silently downgrades the requested mode.
Use a restricted sandbox for selected inputs. Native tools disabled by that
contract remain disabled; approval policy does not enable them.

## Evidence

`tests/test_execution_access.py` exercises real subprocess filesystem reads
and writes through both SDK transports with synthetic local fixtures. It also
checks selected input isolation and policy observations. Instance, queue, CLI
and browser tests cover persistence, overrides and native continuity.
These checks establish adapter behavior, not live upstream acceptance.
Real bundled runtime experiments are recorded in
[native Codex acceptance](../development/native-codex-acceptance.md).

Native configuration reference:
[Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference).
