# Mobile login through GrantBridge

Owner requirement: all authentication passes through GrantBridge, and the engine
login plus consent must be completable from a phone. This document is the single
cross-repository contract for that case. GrantBridge's README points here; Fullbrain
reads only the AgentBridge fields below.

## Why a phone cannot finish the desktop flow

CLIProxyAPI 7.3.16 registers fixed loopback redirect URIs with the providers:
`http://localhost:1455/auth/callback` (Codex) and `http://localhost:54545/callback`
(Claude). Neither provider offers a device-code flow through CLIProxyAPI; only Grok
returns a `user_code`. After consent the provider therefore redirects the phone to a
loopback address that dials nothing on the phone. The phone never has to reach ports
1455 or 54545: the redirect is either handed back to the broker origin, or a browser
hosted next to the sidecar receives it.

```text
same_host (desktop, unchanged)
  browser on the sidecar host -> provider -> http://localhost:1455|54545 -> CLIProxyAPI

isolated (desktop, clean session; same wire request as same_host)
  host process opens authorization_url in a fresh disposable Chromium profile
  (agentbridge.auth_browser.IsolatedAuthBrowser) -> provider -> loopback -> CLIProxyAPI

mobile + mode=browser
  phone -> https authorization_url (provider) -> consent
  phone lands on dead http://localhost:1455|54545/...?code=&state=
  phone -> broker origin: Fullbrain UI paste -> accounts.login.callback
                          or GrantBridge GET|POST /oauth/proxy/callback (owner-bound)
  broker -> auth.proxy_callback -> CLIProxyAPI /v0/management/oauth-callback
  AgentBridge status/check/complete as usual

mobile + mode=hosted (long-lived GrantBridge host with a coordinator)
  AgentBridge -> auth.proxy_start{browser:'mobile', mode:'hosted', owner}
  GrantBridge starts a hosted Chromium at the authorization URL
  phone -> viewer_url (https, GrantBridge origin, same owner session) -> consent
  hosted browser -> http://localhost:1455|54545 on the sidecar host -> CLIProxyAPI
  AgentBridge status/check/complete as usual
```

## AgentBridge request fields (`accounts.login.start`)

| Field | Type | Values | Notes |
| --- | --- | --- | --- |
| `browser` | string | `same_host` (default), `isolated`, `mobile` | Any other value fails with `invalid_request`. |
| `mode` | string | `browser` (default), `hosted` | `hosted` requires `browser: "mobile"`; otherwise `unsupported_operation`. |
| `owner_ref` | string | identifier, 1-128 chars | For `mobile`, set it to the phone's GrantBridge owner digest (hex sha256 of its `lab_session` token). GrantBridge binds the viewer and the callback route to it. |

The same-host request and its GrantBridge wire message are unchanged. `isolated` shares
that wire message: it only records that the host completes the login in a fresh,
disposable browser profile with no cookies or provider session, so the person types the
account they mean instead of silently reusing one already signed in. AgentBridge never
opens a browser itself; the process that owns the desktop (the playground, a CLI, or the
Fullbrain API on the brain host) launches `agentbridge.auth_browser.IsolatedAuthBrowser`
when the attempt is `awaiting_user`, stops it when the attempt leaves that phase, and the
profile is deleted when the browser exits. `available()` is false without a display or
Chromium (for example inside Fullbrain's sandboxed worker), so the host falls back to a
link the person opens elsewhere. For `mobile` AgentBridge adds `browser`, `mode` and
`owner` to `auth.proxy_start`.

## AgentBridge response fields

Every login attempt projection (`start`, `status`, `check`, `complete.attempt`,
`cancel`) now carries `browser` and `mode` as sent. In addition:

| Field | Type | Present when | Source |
| --- | --- | --- | --- |
| `authorization_url` | https URL ≤ 16384 | `mode: "browser"`; omitted for `hosted` | GrantBridge `authorizationUrl` |
| `viewer_url` | https URL ≤ 2048 (http only on loopback) | `mode: "hosted"` | GrantBridge `viewerUrl` |
| `user_code` | string | Grok | GrantBridge `userCode` |
| `error.code` | string | terminal failures | GrantBridge row `error.code` |
| `identity.email` | string | `failed` with `error.code: identity_changed` on a new account | The identity the provider returned when it differs from the requested `email`; the credential is retired, never bound |

Start fails before dispatch with `hosted_browser_unavailable` when the GrantBridge in
use cannot run a hosted browser (the bundled stdio adapter never can), and with
`invalid_browser` when GrantBridge rejects the location. If GrantBridge answers a
`mobile` start without echoing `browser`/`mode`, without an https URL (browser mode) or
without `viewerUrl` (hosted mode), AgentBridge cancels the sidecar session and fails
the attempt with `provider_protocol_error`. A hosted login that ends on the host
reports `failed` with `error.code` `browser_busy`, `browser_closed` or
`hosted_browser_unavailable`.

## GrantBridge JSON read by AgentBridge

`auth.proxy_start` result: `id` (string, the CLIProxyAPI state), `provider`,
`status`, `authorizationUrl` (string), optional `userCode`, and for `mobile` the
echoed `browser` (string) and `mode` (string); `viewerUrl` (string) for `hosted`.
`auth.proxy_status` result: `id`, `provider`, `status`, optional `error.code`
(string). Additive fields are ignored; nothing else is persisted.

## GrantBridge phone-facing return

`GET|POST /oauth/proxy/callback` on the GrantBridge origin (module
`src/agentbridge-proxy-routes.mjs`, mounted by the host). Input: `code` and `state`
query fields, or a JSON body `{redirect_url}` holding the pasted loopback URL. The
state must belong to a login whose `owner` equals the caller's session digest, is
consumed once, is checked against the provider's fixed port and path, is never used
as a redirect target (the route only redirects to `/?callback=returned` or
`/?callback=error`) and is answered with `Cache-Control: no-store`. The code is
relayed to CLIProxyAPI and never stored; store rows keep only the state hash.

## What Fullbrain v2 must change

- `backend/fullbrain/adapters/agentbridge/login.py`: map the default desktop
  transport to `browser: "isolated"` and launch `agentbridge.auth_browser` from the
  API process on the brain host (the sandboxed worker has no display), keeping the
  person's own browser as an explicit fallback (`same_host`); map a phone transport
  to `browser: "mobile"` with `mode: "browser"` or `"hosted"`; read `viewer_url`
  (https, GrantBridge origin) when `mode` is `hosted` and stop requiring
  `authorization_url` in that case; accept `browser_busy`, `browser_closed` and
  `hosted_browser_unavailable` as login error codes; on a `check` that fails with
  `identity_changed`, read `status` and show the failed attempt's `identity.email`
  domain next to the expected one.
- `backend/fullbrain/adapters/agentbridge/client.py`: `PIN_SHA256` must follow the
  new immutable artifact built from this AgentBridge revision.
- `tests/fixtures/agentbridge_protocol.json`: refresh `files[]` digests
  (`authentication.py`, `auth_contract.py`, `grantbridge.py`, `capabilities.py`,
  `commands/parser.py`, `bundle/lock.json`, bundled GrantBridge copies),
  `immutable_artifact_sha256`, `manifest_sha256` and `base_commit`; operations are
  unchanged.
- UI: the paste path already exists (`accounts.login.callback`); add the phone
  entry point that opens `authorization_url` or `viewer_url` and, for browser mode,
  offers the paste field. Issue the phone's GrantBridge session and pass its digest
  as `owner_ref` when the hosted viewer or the GrantBridge callback route is used.

Fixture coverage: `tests/test_mobile_login.py`, `tests/test_isolated_login.py` and
`tests/test_auth_browser.py` here and `test/agentbridge-proxy-mobile.test.mjs` in
GrantBridge. Live provider acceptance from a phone remains pending.
