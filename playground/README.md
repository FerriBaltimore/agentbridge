# AgentBridge playground

This local browser client exercises the public `Bridge` SDK. The HTTP server
only translates browser requests into SDK calls. It does not import AgentBridge
storage, proxy adapters or GrantBridge directly.

## Run

Install AgentBridge into the current Python environment, then run from the
repository root:

```bash
python -m playground.server --root .agentbridge --workspace-path . --port 8765
```

Open `http://127.0.0.1:8765/`. The server binds to loopback and requires a
same-origin mutation header. Install the `cliproxy` CLIProxyAPI executable
and the GrantBridge adapter on the local machine. AgentBridge finds `cliproxy`
on `PATH` or beside the active Python executable; set
`AGENTBRIDGE_CLIPROXY_BIN` for a different location. Set
`AGENTBRIDGE_GRANTBRIDGE_ROOT` if GrantBridge is outside its standard
location. Account sign-in asks only for a provider and display name.
AgentBridge creates an isolated local proxy connection for each account and
uses GrantBridge to coordinate browser authorization. Its generated keys and
local endpoint are never entered in the browser form.

## What to inspect

- Add a provider account through the GrantBridge browser flow, then inspect
  status, exact observed models and usage. Missing usage stays unknown.
- Choose a provider and model in Chat. Automatic routing balances eligible
  accounts within the selected provider; an account can also be pinned.
- Reasoning and context controls appear only when the proxy client model API
  reports them. Context values are maximum token counts reported for the
  selected model and route.
- The permission selector follows SDK capabilities. Choose "Ask before actions"
  to test an interactive provider request, then allow or deny it from Activity.
- Send a message, inspect recorded events, stop a turn, and review its output.
- Remove an account to retire its local route. AgentBridge preserves historical
  conversations; this action does not revoke the upstream OAuth credential.

Browser tests use simulated local proxy responses, GrantBridge and Codex
processes. They do not discover real accounts or verify live provider OAuth
or model acceptance. Run them with `python -m pytest tests/test_playground_browser.py`
when Playwright Chromium or Chrome is installed.
