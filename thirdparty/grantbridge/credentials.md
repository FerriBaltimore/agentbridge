# Credential and security rules

## Where GrantBridge stores state

GrantBridge receives a configurable `dataDir`. When the host does not set one,
the current default is `~/.local/state/grantbridge`. The directory is created
with mode `0700`. Its current layout is:

```text
<dataDir>/
  grantbridge.sqlite       attempt state, owners, statuses and evidence
  vault.key                32-byte local AES key, mode 0600
  vault/*.json             AES-256-GCM encrypted records, mode 0600
  profiles/<attempt-id>/   isolated provider and browser home, mode 0700
  settings.json            non-secret configuration and configured file paths
  google-client.json       optional Google client configuration, if imported
  mcp-client.json          optional MCP configuration, if imported
```

`grantbridge.sqlite` contains the state needed to resume or report an attempt,
but provider token values are kept in `vault/*.json`. The vault encrypts each
record with AES-256-GCM and authenticates the record name as associated data.
The key is stored beside the vault, so this protects files from casual reading,
not a process or user that can read the complete data directory. The host must
therefore protect the directory and its service account.

The `profiles/<attempt-id>` directory is different from the vault. It is the
working home of the provider process and may contain provider-native login
files, a Chrome profile, cookies, callback scratch files and work data. The
provider owns the exact native filenames. GrantBridge isolates this home by
setting `HOME`, `CODEX_HOME` and `CLAUDE_CONFIG_DIR` (and XDG directories) to
the attempt profile. The hosted Chrome profile is also server-side; it is
never copied to the phone.

## What is stored for each provider

| Provider or flow | Secret location after authorization | How it is used |
| --- | --- | --- |
| Claude native login | Provider files below `profiles/<id>/.claude` | A fresh Claude CLI process is started with `CLAUDE_CONFIG_DIR` pointing there. |
| Codex native login | Provider files below `profiles/<id>/.codex` | A fresh Codex app-server is started with `CODEX_HOME` pointing there and reads the account again. |
| Cursor | Encrypted `vault/credential_<id>.json` record containing the SDK key and identity | A probe or inference worker reads it inside GrantBridge. The current login requests a 24-hour key TTL; it is not assumed to be renewable OAuth. |
| Google OAuth | Encrypted `vault/credential_<id>.json` record containing access and refresh tokens | The callback exchanges the code; the Google library refreshes when necessary during the later check and the updated credentials are saved. This adapter does not currently use the generic OAuth SQLite lease. |
| Configurable OAuth/OIDC | Encrypted `vault/oauth_tokens_<id>.json` plus encrypted profile/session records | State and PKCE verifier are checked on callback; access tokens are refreshed and rotated when supported. |
| MCP OAuth | Encrypted `vault/mcp_<id>.json` record | MCP access and refresh tokens are used under the same serialized lease rules. |

The dedicated Google adapter reads its client JSON from a configured
server-side file. When imported through the standalone setup, this file is
written with mode `0600`, without vault encryption. Configurable OAuth profiles
store their client secrets in the vault. These are application credentials,
separate from the user's resulting tokens.

## Authorization data flow

1. The host authenticates its operator and calls GrantBridge with an opaque
   owner identifier. The owner value is not an email and is not an access
   token.
2. GrantBridge creates an attempt row in SQLite and an isolated profile
   directory. It starts the provider's native process or standard OAuth flow.
3. The provider URL is returned as public attempt state. The operator may open
   that URL on a phone. The phone only renders the provider page or the
   server-hosted browser; it does not receive the resulting credential.
4. The provider completes through its callback, device polling, native CLI
   process or server-hosted browser. GrantBridge handles standard OAuth checks;
   native adapters delegate protocol validation to the provider CLI/SDK.
   Available identity evidence differs by provider; MCP does not generally
   return a user identity.
5. The host requests a separate check, or opts into `autoCheck`. Claude's check
   confirms local profile loading only; Codex checks the account and rate-limit
   endpoint. Google, OAuth and Cursor perform their configured authenticated
   request. Neither authorization nor these checks prove model execution.
6. A future AgentBridge adapter will activate the result as a stable account:
   a Claude/Codex account points at the native profile home, while Cursor and
   other token APIs use a private resolver. The current repositories do not
   perform this activation automatically.

The application's mobile UI must not receive server access tokens, refresh
tokens or Cursor keys. The provider may set its own login cookies in the
phone's browser; an authorization code may pass through a browser callback or
the provider's manual-code fallback. Codes must not be logged or persisted in
AgentBridge state, prompts or events.

## Never persist values in AgentBridge

The following must not appear in AgentBridge SQLite, JSON-RPC responses,
prompts, logs or event data:

- OAuth access tokens.
- OAuth refresh tokens.
- Cursor API keys.
- Client secrets.
- Authorization codes.
- Raw provider error bodies that may contain request credentials.

The planned AgentBridge account configuration can contain an environment
variable name, an opaque GrantBridge credential reference or a server-local
native home path. The current `Account` model supports the environment-name
and native-home forms only; it has no GrantBridge credential-reference field
yet. It must never contain the value behind any reference.

## Native homes

Native homes must be isolated per account and have restrictive permissions.
The account registry must reject two account IDs pointing at the same engine
and home. Reauthentication must use the same account identity or create a new
account. A failed relogin must not replace the working native files.

The worker should receive only the provider-specific environment needed for
its account. It must not inherit unrelated provider credentials, MCP
configuration or another account's home.

## Cursor

Cursor is different from Claude and Codex in this design. Its SDK uses an API
key, while AgentBridge's current `Account` model stores an environment
variable name rather than a key. GrantBridge should therefore expose a
credential reference and resolve it only for the lifetime of the worker. A
future implementation can use a Unix pipe or inherited descriptor. It must not
put the key in AgentBridge's persistent store, prompts or an ordinary command
line argument. A protected temporary file is a fallback and must be deleted
after the child exits.

## Ownership and authorization

The `owner` sent to GrantBridge is an opaque host identifier. It is not an
email and does not grant access by itself. The host authenticates the operator
and enforces which account they may connect or use. GrantBridge binds callbacks
and attempt reads to that owner.

## Remote browser

The remote browser is a view and input transport. The phone authenticates the
browser profile owned by the server. It does not authorize the phone as a
separate AgentBridge account, and it does not move provider credentials to the
phone. The server-side browser profile and the native provider profile must be
treated as secrets-bearing state and cleaned up according to the host's account
retention policy.
