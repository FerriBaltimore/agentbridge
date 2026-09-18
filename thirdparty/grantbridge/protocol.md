# Adapter protocol

This is the proposed local protocol between AgentBridge and a GrantBridge
adapter. It is not implemented yet. The transport should be JSON-RPC 2.0 over
stdin/stdout, matching AgentBridge's existing public transport and avoiding a
network listener during development.

The Node sidecar owns one GrantBridge instance and one configured data
directory. The Python parent owns the AgentBridge account registry and the
user-facing command. The sidecar must run for the lifetime of pending attempts;
a restart reconciles them as interrupted unless the provider flow can safely
resume.

## `auth.start`

Request:

`json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "auth.start",
  "params": {
    "account_id": "development-codex",
    "engine": "codex",
    "browser": "same_host",
    "request_key": "client-generated-idempotency-key"
  }
}
`

The adapter derives the owner from the authenticated local host session. A
caller must not be able to choose an arbitrary owner string to access another
owner's attempts.

Response:

`json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "attempt_id": "opaque-attempt-id",
    "provider": "codex",
    "status": "starting",
    "authorization_url": null,
    "expires_at": 1760000000000
  }
}
`

`starting` is normal because native providers may need time to publish their
URL. The client polls `auth.get` until it receives `awaiting_user`,
`authorized`, `failed`, `cancelled`, `expired` or `interrupted`. The result may
contain a device code when the provider requires one. It must never contain an
access token, refresh token, client secret or raw credential file.

`request_key` is scoped to the owner and operation. Repeating it returns the
same attempt result instead of starting a second login. The adapter validates
provider, engine, browser mode and all size and deadline limits before spawning
a process.

## `auth.get`

`json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "auth.get",
  "params": {"attempt_id": "opaque-attempt-id"}
}
`

The result contains the safe attempt projection, identity and verification
state. A future activation result may include a server-local native-home path
only when the host explicitly requested one and the path is inside the
configured account root. For Cursor and token APIs it should return an opaque
`credential_ref`, never the key.

## `auth.cancel`

`json
{"jsonrpc":"2.0","id":3,"method":"auth.cancel","params":{"attempt_id":"opaque-attempt-id"}}
`

Cancellation is explicit. The adapter should make a repeated cancel safe at the
protocol boundary even though the current GrantBridge method reports
`already_finished` for a terminal attempt. Cancelling a pending attempt must
not remove an existing authorized account or its conversations.

## `auth.check`

`json
{"jsonrpc":"2.0","id":4,"method":"auth.check","params":{"account_id":"development-codex"}}
`

The check launches a fresh provider process or provider request and returns
structured observations such as local credential presence, authenticated
identity, quota access or model execution. It is not a model run and does not
grant permission to execute a business operation.

## `auth.activate` and relogin

Activation is the missing bridge between a successful attempt and an
AgentBridge account. A future `auth.activate` must:

- verify the provider identity against the requested stable account;
- atomically publish a new credential generation only after checks pass;
- retain the old generation until the new one is usable;
- return a stable account reference, not a secret;
- allow a failed relogin to leave the current account working.

A fresh attempt ID must not become the permanent AgentBridge account ID.

## Credential handoff

The first implementation should use two provider-specific handoff modes:

1. **Native home reference:** GrantBridge provisions or updates an isolated
   Claude or Codex home. AgentBridge stores only the server-local path and
   provider identity, then sets `CLAUDE_CONFIG_DIR` or `CODEX_HOME` for the
   worker.
2. **Short-lived secret channel:** Cursor credentials remain in GrantBridge's
   encrypted vault. At worker start, the adapter resolves `credential_ref`
   through a private pipe or inherited file descriptor and injects the value
   into the child environment. The value must not enter AgentBridge SQLite,
   argv, logs or events.

The adapter must define ownership and cleanup for both modes. A path reference
is not a credential export and must be rejected if it points outside the
configured account root.
