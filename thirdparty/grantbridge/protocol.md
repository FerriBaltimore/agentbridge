# Private GrantBridge adapter protocol

AgentBridge consumes the JSON-RPC 2.0 stdio adapter in GrantBridge, currently
reviewed at revision `798ef3518778a82953b2d2365ee270271cad2f7a`.
The executable sources are `src/agentbridge-protocol.mjs`,
`src/agentbridge-credentials.mjs` and `scripts/agentbridge-adapter.mjs`.
This private boundary is distinct from AgentBridge's public snake_case RPC.
It does not establish compatibility with arbitrary future GrantBridge revisions.

## Framing and ownership

One JSON object per line. Each response has `jsonrpc: "2.0"`, the request `id`
and exactly one of `result` or `error`. The error envelope carries a numeric
JSON-RPC code and may carry `data.code`. Unknown private error codes become
`grantbridge_failed`; provider error bodies are not exposed or persisted.
Malformed matching envelopes become `provider_protocol_error`.

Every account operation requires `owner`. It is AgentBridge's durable attempt
owner, not the account name or a new owner on every retry. The local host chooses
the adapter executable and its private data directory; public RPC cannot change
these references. `health` returns the service and version. `auth.close` closes
the sidecar; pending native work follows GrantBridge's interrupted-state rules.

## Start, inspect and reconcile

```json
{"jsonrpc":"2.0","id":1,"method":"auth.start","params":{"owner":"owner-ref","engine":"codex","mode":"browser","browser":"same_host","request_key":"durable-key","auto_check":false}}
```

The result uses native field names:

```json
{"jsonrpc":"2.0","id":1,"result":{"id":"opaque-attempt-id","provider":"codex","status":"starting","authorizationUrl":null,"expiresAt":1760000000000}}
```

`auth.get` takes `attempt_id` and `owner`. `auth.find` takes `request_key` and
`owner`, returning the existing attempt or null. It reconciles a lost start
response without creating another provider login.

Known remote states: `starting`, `awaiting_user`, `exchanging`, `authorized`,
`failed`, `cancelled`, `expired`, `interrupted`, `revoked`, `replaced`.
`verified`, `bound` and `usable` are AgentBridge states, never accepted as remote
GrantBridge authorization evidence. Unknown states or mismatched provider/ID
produce `provider_protocol_error`; a saved verified attempt is invalidated on
such a refresh and cannot subsequently activate using its older observation.

The public projection copies bounded identity fields and the documented
verification observations, timestamps, authorization URL and device user code.
Opaque added fields, nested credentials and error messages are dropped. New
optional fields do not invalidate an otherwise compatible attempt. Additive
fields needed by the product must be added deliberately with regression tests.

## Check, complete and cancel

`auth.check` takes `attempt_id`, `owner` and optional `inference` (boolean).
The result is an attempt with `verification.freshProcess`, optional
`verification.inference`, and `checking`. AgentBridge verifies only an
`authorized` attempt whose check is no longer running and whose fresh-process
result is explicitly `passed`. `loaded_only` does not verify usability.
An inference probe is explicit and consumes provider usage.

`auth.activate` takes `attempt_id` and `owner`. Its result includes
`attempt_id`, `provider`, `identity` and either a native `home` for Codex/Claude
or Cursor `credential_ref` (`provider`, `attempt_id`) and `expires_at_ms`.
AgentBridge validates provider, attempt and expected identity before atomically
binding the account. Relogin preserves the account ID and cannot silently change
its identity. The native home is a trusted local-host reference, never a public
RPC field. GrantBridge owns the provisioned directory under its configured root.

`auth.cancel` takes `attempt_id` and `owner`. Repeating cancellation is safe.
Existing authorized provider grants are not revoked by local cancellation.
A cancelled AgentBridge attempt cannot later bind, even if a remote check ends.

`auth.submit_code` is a private protected input taking `attempt_id`, `owner`
and `code`. It is not exposed by AgentBridge's public dispatcher.

## Private credential handoff

`auth.credentials` takes `attempt_id` and `owner`; only the trusted execution
adapter may call it. For Cursor its result contains `provider`, `api_key` and
`expires_at_ms`. This result intentionally carries a credential in the private
pipe, never in public RPC, SQLite, command arguments, logs or events. GrantBridge
checks the verified attempt, credential expiry, identity and supported backend.

Codex/Claude workers use an explicit isolated home with `CODEX_HOME` or
`CLAUDE_CONFIG_DIR`. Cursor workers resolve the bound reference at execution time
and receive the credential through a private environment handoff. Ambient
operator credentials never substitute for an explicit account binding.
