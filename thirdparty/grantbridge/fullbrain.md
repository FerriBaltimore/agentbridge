# Future Fullbrain host

Fullbrain should use AgentBridge's public login methods rather than call
GrantBridge or CLIProxyAPI directly. It supplies the authenticated user and
account scope, displays safe attempt state and keeps mission, workspace and
business policy outside the authentication layer.

1. Fullbrain prepares or selects an empty, dedicated local CLIProxyAPI sidecar.
2. It calls `accounts.login.start` with provider, name, proxy URL and key
   environment variable references and persists `attempt_id` and `owner_ref`.
3. GrantBridge coordinates provider OAuth through the sidecar Management API.
   CLIProxyAPI owns the credential. Fullbrain displays the safe authorization
   challenge but never receives a token or management key value.
4. Fullbrain polls status, requests a fresh check and completes the account
   only after AgentBridge verifies one identity and the model catalogue.
5. Fullbrain chooses a model; AgentBridge selects a compatible account and
   always runs Codex through that account's proxy route.

The initial browser mode assumes browser and sidecar are on the same host.
Authenticated remote browser presentation and callback routing need separate
implementation and live acceptance. Fullbrain must not read private SQLite,
GrantBridge vault, CLIProxyAPI auth files or browser profiles.
