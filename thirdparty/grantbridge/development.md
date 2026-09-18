# Development workflow

## What works today

AgentBridge can still consume an already authenticated account. It can register
an isolated native home and execute with it:

```bash
agentbridge accounts add \
  --engine codex \
  --home /path/to/codex-home \
  --name "Development Codex"

agentbridge accounts status "Development Codex" --refresh
agentbridge accounts usage "Development Codex" --refresh
```

The same reference model exists for Claude. Cursor currently uses an environment
variable reference for its API key. The values themselves must stay outside
AgentBridge state.

This remains useful when another host performs login. The native provider login
can also be performed directly by the local GrantBridge adapter.

## Local GrantBridge login

The next host command should be:

```bash
agentbridge accounts login --engine codex --name "Development Codex" \
  --grantbridge-root /home/ferran/grantbridge
agentbridge accounts login --engine claude --name "Development Claude" \
  --grantbridge-root /home/ferran/grantbridge
```

The command should:

1. Generate an internal account identifier without exposing it in the command.
2. Ask the GrantBridge sidecar to start the provider attempt.
3. Print the authorization URL. Opening it is an explicit user action; an
   opt-in local browser helper may open it on the same host.
4. Poll the attempt and show only safe status, identity and next action.
5. On success, activate a native-home reference in AgentBridge for Codex or
   Claude. Cursor remains GrantBridge-vault-backed until a private resolver is
   available.
6. Run a fresh-provider check before reporting the account as authenticated.

The command must not ask the user to paste an OAuth code when the provider can
complete through a browser callback. If a provider genuinely returns a device
code, the UI can show that code as a provider challenge.

## Local and remote URLs

GrantBridge accepts a local HTTP origin on loopback or an HTTPS origin for
remote access. The host supplies `origin` (or `GRANTBRIDGE_ORIGIN`) and mounts
the callback routes. Setting an origin does not create a tunnel, HTTPS service
or browser viewer; the embedding application supplies those.

| Operator location | Completion path |
| --- | --- |
| On the execution machine | A supported loopback callback can return to the native process on that machine. |
| Phone or another computer, standard OAuth | The provider returns to the host's reachable HTTPS callback registered for that OAuth client. |
| Phone or another computer, native loopback login | Use the server-hosted browser so that its loopback callback reaches the server's native process. |
| Provider-supported device or polling flow | The server waits for provider confirmation while the operator consents in another browser. |

All paths leave the resulting execution credentials in the configured
server-side `dataDir`, regardless of where the operator opens the UI. Local
and remote use should be transparent to the operator once the host adapter is
implemented. They are not interchangeable redirect URLs: the provider's
callback registration and the flow's originating session must match.

## Mobile during development

AgentBridge can remain the host even when the user operates from a phone. The
sidecar returns an authorization URL. The host either:

- exposes that URL through an authenticated development web surface, or
- uses GrantBridge's hosted browser and exposes its browser frame and input
transport.

The packaged GrantBridge SDK has hosted-browser mechanics, but its public
callback router does not include the standalone lab's viewer and input routes.
AgentBridge's host adapter still needs that authenticated presentation layer;
`browser: mobile` alone does not provide remote access.

The phone authenticates the server-side provider session. It does not become
the execution account and it does not receive a provider token. A loopback
callback opened directly on the phone returns to the phone, so native flows
that require a server-local callback must use the hosted server browser,
provider polling or a valid server HTTPS callback.

## Shutdown and recovery

An authentication attempt has its own ID and deadline. Cancelling the
AgentBridge command must cancel the GrantBridge attempt. Restarting the host must
reconcile the attempt before presenting it as pending. A failed or interrupted
login must not silently start a new login or change the selected account.

The sidecar is one long-lived process for the attached login command. Its
shutdown closes child processes and records an interrupted state; it does not
log out provider accounts.
