# AgentBridge

AgentBridge v2 is a Python SDK, CLI and JSON-RPC stdio API for durable coding
agent conversations. Codex is the sole execution engine. Every turn goes through
a dedicated local CLIProxyAPI sidecar for the selected upstream account.
AgentBridge chooses a compatible account for the requested model before each
automatic turn and records the decision. It never changes accounts during a
turn or silently retries an uncertain outcome.

## Account flow

There is one account onboarding flow: `accounts login`. GrantBridge coordinates
the provider's browser authorization through CLIProxyAPI's Management API.
AgentBridge prepares an empty, dedicated loopback sidecar for each account by
default. CLIProxyAPI owns and renews the upstream credential in its isolated
authentication directory. AgentBridge stores only route and key references,
plus safe identity and model evidence. Generated proxy keys pass over a
private local channels during login and execution; they never enter the
AgentBridge database or public account projections. No account is created
until the sidecar reports one verified upstream identity
and a usable model catalogue.

Install the AgentBridge wheel for Linux x86_64 or ARM64. It includes pinned
CLIProxyAPI, Codex, the GrantBridge proxy adapter and Node.js; no separate
runtime installation or binary environment variable is needed. On first use,
AgentBridge verifies and extracts these resources into its private state root,
outside the model-writable workspace. Managed sidecars require Linux `memfd`
support. Then run:

```bash
agentbridge accounts login --provider codex --name "Personal Codex"
```

`codex`, `claude` and `grok` are the initial provider choices. AgentBridge
chooses the sidecar port, generates its local access keys and reconnects the
managed route after a client restart. Advanced callers with an existing,
isolated proxy may pass `--proxy-base-url`, `--proxy-key-env` and
`--proxy-management-key-env` together; these are internal route references in
the same GrantBridge OAuth flow, not a second account type. Use
`accounts login-start` for a restart-safe asynchronous attempt, followed by
`login-status`, `login-check`, `login-complete` or `login-cancel`.
`login-complete` commits the account only after fresh verification. The
current browser flow assumes that the browser and the sidecar run on the same
host; remote browser acceptance is pending. The provider OAuth flow itself
still needs controlled live acceptance. See the
[v2 routing contract](docs/interface/v2-model-routing.md) and
[implementation inventory](docs/interface/implementation-status.md).

The CLI also provides `accounts list`, `accounts status`, `accounts usage`,
`accounts check`, `accounts pause`, `accounts resume` and `accounts delete`.
Pause excludes an account from new routing while preserving its login and
current turns. Delete retires the local route and
preserves its history; it does not remove the upstream proxy credential.
Use `accounts delete --account-id ID` for an exact retry after an uncertain
stop or a reused account name.
Public CLI text is English; account names are preserved as entered. There is
no independent `accounts add` onboarding route.

## Conversation flow

```python
from agentbridge import Bridge

with Bridge() as bridge:
    instance = bridge.instance_create(model="your-model-id", workspace_path=".")
    accepted = bridge.message_create(instance["id"], "Inspect the tests.")
    print(bridge.run(accepted["turn_id"]).wait())
```

The default state directory is `${XDG_STATE_HOME:-~/.local/state}/agentbridge`
with private permissions. An explicit root must stay outside the workspace.
If the project has `.agentbridge/bridge.sqlite3`, stop its active work and
move that state deliberately before using the new default; AgentBridge raises
`state_migration_required` rather than silently ignoring existing accounts.
For a `workspace-write` turn, keep the AgentBridge installation and private
state directory outside the writable workspace.

`models.list` exposes exact configured model IDs and marks which accounts have
fresh local proxy observations for each ID. When available, it also reports
per-account reasoning levels, input modalities and context maximums from the
proxy client model API. The caller chooses a model, may restrict automatic
routing to one provider, or may pin an account. Automatic routing keeps account
affinity across turns, choosing another eligible account after verified quota
exhaustion or an explicit eligibility change. A lower usage percentage on
another account does not break affinity. A change between turns starts a
fresh Codex thread with bounded portable context and explicit omissions. The
selected proxy endpoint remains fixed for the complete turn, including tool
calls. Missing or stale quota stays unknown, never zero.

Use `routing_mode="automatic", account_ref="Example"` to choose the initial
account while retaining automatic affinity. `routing_mode="pinned"` keeps a
selected account without automatic failover. `instances.update` can change
the mode and account between turns. Temporary rate limits and busy accounts
preserve affinity. See [account affinity and cache](docs/interface/account-affinity.md)
for routing rules and the limits of native cache telemetry.

Records predating v2 remain readable but cannot create accounts or start turns.
Their evidence does not establish acceptance of the proxy path.

AgentBridge records requests, normalized events, usage evidence and unknown
outcomes in SQLite. Stop is explicit cancellation. Recovery never silently
replays work or changes accounts. Credentials, raw provider errors and private
reasoning are not persisted. The package is independent of Fullbrain.
`bridge.instance_delete(instance_id)` removes an ordinary conversation's local
history, exported context archives and private runtime data after its turn and
owned processes have ended.

## Local playground

The isolated [playground](playground) is a browser UI for exercising the public
AgentBridge SDK. It uses the same account login, local route observations and
conversation APIs as other clients. The browser receives sanitized SDK data;
proxy keys stay inside the local AgentBridge process and supervisor.

From this repository, after installing AgentBridge into the current Python
environment, run:

```bash
python -m playground.server --workspace-path . --port 8765
```

Open `http://127.0.0.1:8765/`. The server binds only to loopback. Accounts
can be added through GrantBridge OAuth, inspected for current usage, paused,
resumed and removed from AgentBridge. Removal retires the local route and preserves its
history; it does not revoke or delete the upstream CLIProxyAPI credential.
Model, provider, reasoning and context controls appear when the local proxy
reports them. Unknown usage and unavailable controls stay visible as unknown.
Chat lets you delete a conversation from its list after confirmation.

## Interface and development

AgentBridge provides [persistent conversation queues](docs/interface/message-queues.md)
through the SDK, JSON-RPC and `agentbridge queues`. Add, list, remove and reorder
pending messages; pause or resume dispatch; send a pending message immediately
with explicit steering or interruption. Use `messages.create(delivery="queue")`
or `Bridge.queue_add` to enable queued delivery. The compatibility default for
`messages.create` remains immediate admission with a busy error when occupied.
Queued turns run independently of the client, and their message IDs survive
reconnection. Live provider acceptance remains separate from fixture coverage.

The [interface contract](docs/interface/README.md) describes accounts, models,
instances, messages, turns, usage and events. The Python SDK, CLI and JSON-RPC
stdio API expose the same account flow. AgentBridge's managed CLIProxyAPI
sidecars bind to loopback; the SDK does not expose a public network server.
Fullbrain's v1-to-v2 work is mapped in the
[migration guide](docs/interface/fullbrain-v2-migration.md) and
[implemented RPC reference](docs/interface/fullbrain-v2-rpc.md).
The [bundled runtime guide](docs/development/bundled-runtime.md) describes
platform wheels, pinned sources, offline execution and upgrade behavior.
The [CLIProxyAPI update guide](docs/development/cli-proxy-api-update.md)
describes the reviewed updater.
For a JSON-RPC caller:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"capabilities","params":{}}' \
  | agentbridge rpc
```

Every repository text file is limited to 450 physical lines. During development:

```bash
python tools/check_repository.py
python -m pytest
python -m pip wheel . --no-deps -w dist
```

Source builds download missing upstream archives into `dist/bundle-cache`,
verify their pinned SHA-256 hashes and package one architecture per wheel.
Installed wheels do not download runtimes. Install the built wheel into a
disposable environment and run the installed example and CLI. Reinstall
delivered SDK changes into this repository's `.venv` and verify them. Updating
a wheel does not restart already-running account sidecars. The repository is
unlicensed until its owner chooses a license; do not publish a package without
that instruction.
