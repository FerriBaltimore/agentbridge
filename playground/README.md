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

After updating the playground or its installed SDK, restart the playground
server and reload the page. Python handlers stay loaded until the server
restarts, while browser assets are read from disk. Chat mutations check the
HTTP API revision before submission and at admission, preserving the draft
when the page and server are incompatible. Stop and approval responses remain
available during an update. Bump `API_REVISION` in both `server.py` and
`static/api.js` when changing the chat request contract incompatibly.

The default state is `${XDG_STATE_HOME:-~/.local/state}/agentbridge`, outside
the project workspace. An explicit `--root` must also be outside the workspace.
If this project already has `.agentbridge/bridge.sqlite3`, stop active turns and
sidecars, then move that directory to the new state location before using the
default. AgentBridge does not silently import or discard the old accounts.

## What to inspect

- Add a provider account through the GrantBridge browser flow, then inspect
  status, exact observed models and usage. Missing usage stays unknown.
- In a new Chat conversation, choose a provider, routing mode, optional
  observed account and model. Automatic routing starts with the least-used
  eligible account unless one is selected, then keeps that account until
  confirmed exhaustion or ineligibility. Pinned routing stays on the selected
  account. Route selectors share one row with widths adapted to each field;
  narrower screens scroll the row horizontally. The information button beside
  Routing mode explains both choices, account affinity and session continuity.
  Open it with a click, tap or keyboard, and dismiss it with Escape, Close or
  a click outside.
- Change the provider, model, routing mode or preferred account between turns
  within the same conversation. These changes retain the same native Codex
  session and history. Inspect route evidence for the selected account; a
  missing or divergent native session blocks continuation explicitly.
- Chat restores the last viewed conversation. Its inline timeline displays
  observed tools, permissions, partial responses and compaction progress while
  a turn runs; private reasoning is not displayed. A local SSE connection
  delivers the SDK's normalized events with sequence-based replay. Polling
  reconciles state if that connection is interrupted.
- Reasoning and context controls appear only when the proxy client model API
  reports them. Context choices use observed default and maximum values that
  are safe for the selected route; Default leaves the override unset. The SDK
  rejects a requested override if fresh route metadata cannot support it.
- More settings offers Read-only, Workspace write and Full access, plus
  No approval prompts or Ask when required. Full access allows host file and
  network access. Save access settings while idle, or send the next message
  to apply pending choices. The choices belong to the conversation, survive
  reloads and retain the same native Codex session across route changes.
  With Ask when required, respond to a native approval request from the chat
  timeline or Activity.
- Send a message, inspect recorded events, stop a turn, and review its output.
- Keep sending while a turn runs: messages enter the persistent queue. Open
  Queue above the composer to move pending messages up or down, remove them,
  or pause and resume automatic execution. Reloading restores the saved order.
  Send now introduces a pending message into the active turn; Stop & send
  cancels the active turn and runs that message next. The composer also offers
  these delivery choices while a turn is active. Stop pauses pending work;
  review the result and resume explicitly. Route and access settings remain
  locked while messages are pending. The SDK owns scheduling and persistence.
- Delete a conversation from the Chat list after confirming. Its local history
  and private runtime data are removed once its turn and owned processes end.
- Remove an account to retire its local route. AgentBridge preserves historical
  conversations; this action does not revoke the upstream OAuth credential.

Browser tests use simulated local proxy responses, GrantBridge and Codex
processes. They do not discover real accounts or verify live provider OAuth
or model acceptance. Run them with `python -m pytest tests/test_playground_browser*.py`
when Playwright Chromium or Chrome is installed. CI installs Chromium and
requires it to launch before running the test suite. The playground itself is
kept in this repository; it is not included in the AgentBridge runtime wheel.
See the [queue UI acceptance record](../docs/development/playground-queues-acceptance.md).
See the [2.3.1 acceptance record](../docs/development/playground-2-3-1-acceptance.md),
the [2.3.0 playground acceptance record](../docs/development/playground-2-3-acceptance.md),
the [2.2.0 record](../docs/development/playground-2-2-acceptance.md),
and the earlier [bundled-runtime record](../docs/development/bundle-acceptance.md).
