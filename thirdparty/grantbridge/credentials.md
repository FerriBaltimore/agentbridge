# Credential and state boundary

AgentBridge v2 has one account authorization flow. The dedicated CLIProxyAPI
sidecar stores and renews each upstream OAuth credential in its private auth
directory. GrantBridge coordinates its Management API browser flow, while
AgentBridge keeps durable login attempt state. AgentBridge stores neither upstream OAuth tokens nor proxy key
values. Historical GrantBridge native homes and vault records may still exist
for older attempts, but they cannot create a new v2 account or execute a turn.

## V2 data flow

1. The host authenticates its operator. AgentBridge creates a durable attempt
   for an empty, dedicated loopback sidecar and an opaque owner reference.
2. `accounts.login.start` receives provider (`codex`, `claude` or `grok`), name,
   proxy URL and environment variable **names** for client and management
   keys. AgentBridge resolves the management key only for the trusted local
   GrantBridge adapter call. The value travels through private stdio, not
   public RPC, argv, SQLite, events or logs.
3. GrantBridge calls CLIProxyAPI's Management API to start OAuth and projects
   only safe authorization URL, status and bounded diagnostic fields. The
   SDK browser and sidecar share the server; the person uses the owned remote viewer.
4. CLIProxyAPI receives the callback and keeps credential material. GrantBridge
   does not export an upstream OAuth token to AgentBridge.
5. AgentBridge reads fresh Management API inventory, identity and models.
   `accounts.login.complete` binds only one stable identity with a usable
   local model catalogue. The proxy client key is read from its environment
   variable only for Codex execution.

CLIProxyAPI data directories, proxy client keys and management keys are
secrets-bearing infrastructure. Use separate restricted auth directories and
ports for separate upstream accounts. The Management API key is required for
login and every route check, pinned or automatic, and must not be given to
Codex. The route must remain fixed throughout a turn; external sidecar
reconfiguration is outside AgentBridge's lock.

## Never persist in AgentBridge

- OAuth access or refresh tokens, authorization codes or browser cookies.
- Proxy client or management key values.
- Raw Management API configuration, raw provider error bodies or private
  reasoning.
- An unverified identity represented as a verified account.

Account records may contain a loopback URL, provider, account name,
`key_env` and `management_key_env` references, and safe fingerprints of
identity evidence. Public account projections omit the URL and key references.
Unknown or stale usage remains unknown, not zero.

## Ownership and remote browser

The host authenticates its user and enforces which account they may connect or
use. GrantBridge's opaque owner scopes attempt reads and callbacks; it is not
an email or access token. The packaged browser channel binds every asset, frame
and input to that owner and attempt. Its browser and temporary database are
removed at the terminal state or SDK shutdown. They are excluded from durable
credential inventory because they never contain the authoritative final tokens.
The verified runtime and active-login capture checks remain required. See
[the browser contract](../../docs/interface/mobile-login.md).

Earlier native provider profiles and GrantBridge vault records document the
previous architecture. Their presence cannot be treated as a v2 proxy
credential or automatically migrated into CLIProxyAPI.

The exact stateless closure shipped in AgentBridge 2.9.1 remains admissible for stored
bound routes. All four historical file hashes, the original private runtime path and its
verification marker must match. This does not admit a custom adapter or a durable data_dir.
The historical fixture is retained under tests/fixtures/grantbridge_291 with its MIT license.
