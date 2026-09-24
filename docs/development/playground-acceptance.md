# Playground acceptance, AgentBridge 2.1.0

Local checks on Linux x86_64, 2026-09-24. The playground server ran from this
repository with AgentBridge 2.1.0 installed from its platform wheel in `.venv`.
The state directory was disposable and outside the workspace. No external
CLIProxyAPI, Codex, Node or GrantBridge paths were configured.

- The five HTTP server tests passed. They exercise the public SDK mapping and
  reject unexpected account login fields.
- All 16 Playwright browser cases passed in headless Chrome. Their provider,
  Codex and GrantBridge processes are simulated; they exercise the UI flow,
  not live provider acceptance.
- With the installed wheel, the browser loaded Overview, Accounts, Chat and
  Activity. An empty state showed zero accounts and conversations and no
  observed models. The add-account dialog listed Codex, Claude and Grok.
  There were no page errors or horizontal overflow at a 390-pixel viewport.
- From that same installed SDK, the UI started authorization for each of
  Codex, Claude and Grok through the bundled CLIProxyAPI and GrantBridge
  adapter. Each showed an authorization link and then confirmed cancellation.
  No authorization link was opened, and the disposable sidecars were stopped.
- All playground JavaScript files passed `node --check`; the repository guard
  passed. CI now installs and launches Chromium before running the suite, so
  a missing browser cannot silently skip these cases.

These observations do not establish completed OAuth, account usage accuracy,
model entitlement, real inference or ARM64 browser acceptance. The playground
is a repository-local SDK client and is not included in the runtime wheel.
