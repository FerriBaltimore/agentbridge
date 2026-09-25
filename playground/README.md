# AgentBridge playground

This local browser client exercises the public `Bridge` SDK. The HTTP server
only translates browser requests into SDK calls. It does not import AgentBridge
storage, proxy adapters or GrantBridge directly.

## Run

Install AgentBridge into the current Python environment, then run from the
repository root:

```bash
python -m playground.server --workspace-path . --port 8765
```

Open `http://127.0.0.1:8765/`. The server binds to loopback and requires a
same-origin mutation header. AgentBridge 2.3.1 includes CLIProxyAPI, Codex,
Node.js and the GrantBridge proxy adapter in its Linux platform wheel; no
separate runtime installation or environment variables are needed. Account
sign-in asks only for a provider and display name. AgentBridge creates an
isolated local proxy connection for each account and uses GrantBridge to
coordinate browser authorization. Its generated keys and local endpoint are
never entered in the browser form.

The default state is `${XDG_STATE_HOME:-~/.local/state}/agentbridge`, outside
the project workspace. An explicit `--root` must also be outside the workspace.
If this project already has `.agentbridge/bridge.sqlite3`, stop active turns and
sidecars, then move that directory to the new state location before using the
default. AgentBridge does not silently import or discard the old accounts.

## What to inspect

- Add a provider account through the GrantBridge browser flow, then inspect
  status, exact observed models and usage. Missing usage stays unknown.
- In a new Chat conversation, choose a provider, then Automatic or a named
  observed account, then a model. Automatic routing balances eligible accounts
  within the selected provider; a named choice pins that account. Start a new
  conversation to choose a specific account when the current one uses
  automatic routing.
- Change the provider and model between turns within the same conversation.
  A pinned account can be switched to automatic routing; its next turn uses
  portable context if the account changes.
- Chat restores the last viewed conversation. Its inline timeline displays
  observed tools, permissions, partial responses and compaction progress while
  a turn runs; private reasoning is not displayed. A local SSE connection
  delivers the SDK's normalized events with sequence-based replay. Polling
  reconciles state if that connection is interrupted.
- Reasoning and context controls appear only when the proxy client model API
  reports them. Context choices use observed default and maximum values that
  are safe for the selected route; Default leaves the override unset. The SDK
  rejects a requested override if fresh route metadata cannot support it.
- The permission selector follows SDK capabilities. Choose "Ask before actions"
  to test an interactive provider request, then allow or deny it from Activity.
- Send a message, inspect recorded events, stop a turn, and review its output.
- Delete a conversation from the Chat list after confirming. Its local history
  and private runtime data are removed once its turn and owned processes end.
- In a Codex account's usage dialog, check earned reset credits and inspect
  their reported expiry. Redeeming one requires an explicit confirmation. The
  browser saves the request key before sending it and offers the same-key retry
  after an uncertain outcome; opening the dialog never redeems a credit.
- Remove an account to retire its local route. AgentBridge preserves historical
  conversations; this action does not revoke the upstream OAuth credential.

Browser tests use simulated local proxy responses, GrantBridge and Codex
processes. They do not discover real accounts or verify live provider OAuth
or model acceptance. Run them with `python -m pytest tests/test_playground_browser*.py`
when Playwright Chromium or Chrome is installed. CI installs Chromium and
requires it to launch before running the test suite. The playground itself is
kept in this repository; it is not included in the AgentBridge runtime wheel.
See the [2.3.1 acceptance record](../docs/development/playground-2-3-1-acceptance.md),
the [2.3.0 playground acceptance record](../docs/development/playground-2-3-acceptance.md),
the [2.2.0 record](../docs/development/playground-2-2-acceptance.md),
and the earlier [bundled-runtime record](../docs/development/bundle-acceptance.md).
