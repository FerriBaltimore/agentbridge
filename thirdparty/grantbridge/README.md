# GrantBridge integration

This directory describes how AgentBridge can use GrantBridge for account authentication while keeping agent execution in AgentBridge. It contains integration documentation only, not a vendored dependency or a working adapter.

Reviewed on 2026-09-18 against AgentBridge `3b216a2` and GrantBridge `5eebb0386e647f062647a3208f2ad49c28eba515`. GrantBridge's repository is [FerriBaltimore/grantbridge](https://github.com/FerriBaltimore/grantbridge). Recheck the [source inventory](current-api.md) when either dependency changes.

The boundary is deliberate:

| Responsibility | Component |
| --- | --- |
| Provider login, OAuth/OIDC/MCP authorization and credential lifecycle where supported | GrantBridge |
| Native agent processes, sessions, runs, events and continuity | AgentBridge |
| User identity, account selection, permissions and presentation | The embedding application, initially AgentBridge's CLI during development |

GrantBridge is a Node.js SDK. AgentBridge is a Python SDK and CLI. AgentBridge should consume GrantBridge through a small local adapter or sidecar rather than reimplementing provider login in Python.

## Current state

The repositories are not fully wired together yet.

- GrantBridge exposes native Claude, Codex and Cursor authorization attempts through `startProvider`, `checkProvider`, `cancelProvider` and `submitProviderCode`.
- GrantBridge has a real-provider smoke test: `npm run test:real`. It starts each installed native adapter, verifies its authorization URL and cancels the attempt without changing an account.
- AgentBridge currently registers references to already prepared account homes or environment variable names. `agentbridge accounts add` does not perform login.
- AgentBridge's provider account observation adapter is implemented for Codex. Claude and Cursor status probes remain unsupported in the current `AccountService`; that does not mean their execution adapters are absent.
- The handoff of a GrantBridge native home or a Cursor credential reference into an AgentBridge account is a planned adapter, not an existing feature.

This distinction matters: the documents below define the integration contract and the development path. They do not claim that `agentbridge accounts login` already exists.

## Recommended shape

During development, AgentBridge itself can be the host application. It launches a local GrantBridge sidecar for authentication, presents the authorization URL to the operator, receives a sanitized result and registers the resulting account reference. Fullbrain can later use the same GrantBridge API through a different host adapter.

```text
AgentBridge CLI or SDK
  |
  | local JSON-RPC over stdin/stdout
  v
GrantBridge adapter process
  |
  | authorization URL, callback and native browser flow
  v
Claude / Codex / Cursor

AgentBridge worker <--- account reference or short-lived credential handoff
```

A separate parent product is unnecessary. AgentBridge can be the parent for the local sidecar. The important separation is between the authentication library and the execution worker, not the number of processes.

Read the documents in this order:

1. [Current APIs and gaps](current-api.md)
2. [Architecture](architecture.md)
3. [Development workflow](development.md)
4. [Adapter protocol](protocol.md)
5. [Credential and security rules](credentials.md)
6. [Implementation sequence](implementation.md)
7. [Acceptance plan](acceptance.md)
8. [Future Fullbrain host](fullbrain.md)
