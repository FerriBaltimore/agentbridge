# Provider login from local and remote browsers

AgentBridge 2.10.0 embeds GrantBridge's browser driver and viewer. The same flow works
from a desktop or phone while the SDK runs on a server without a desktop. CLIProxyAPI
owns the provider authorization and final tokens. AgentBridge still verifies the
account identity and available models before binding the account.

## Start and ownership

Use `accounts.login.start` with `browser="mobile"`, `mode="hosted"`, a stable
`request_key` and the application's authenticated `owner_ref`. Here `mobile` is the
existing transport name; the viewer also works on desktops. Do not derive ownership
from caller-supplied URL parameters or introduce a GrantBridge session cookie.

A successful start returns `browser_transport="rpc"` and `viewer_url="browser.html"`.
This is a fixed SDK asset identifier, not a public URL. Fullbrain projects its own
same-origin, authenticated viewer URL for the owned attempt. Hosted responses omit
`authorization_url`: the SDK opens the provider page automatically.

A hosted start may add the client's screen as `viewport`, an object with exactly
`width` (whole number, 320–1280 CSS pixels), `height` (whole number, 480–1280) and
`scale` (number, 1–3, the device pixel ratio). AgentBridge validates it and forwards it
verbatim in the GrantBridge start request, which renders the hosted page at that size and
density. Anything else, including a `viewport` on a non-hosted login, fails with
`invalid_params` before GrantBridge is contacted. Without `viewport` the request carries
no such key and GrantBridge keeps its default (390×760 at scale 1). Replaying a
`request_key` with a different `viewport` returns the existing attempt unchanged.

The application keeps the same `Bridge` instance (or stdio worker) alive through the
login. Each configuration owns one GrantBridge child. Ending the SDK lifetime cancels
pending browser interactions and removes temporary state. A restarted worker never
adopts or reopens an old browser. It reports an interrupted attempt for explicit recovery.

## Browser channel

RPC: `accounts.login.browser`. Python: `Bridge.account_login_browser`.
Every call requires `attempt_id` and `owner_ref`; snapshot account holds still apply.

| Action | Additional fields | Result |
| --- | --- | --- |
| `view` (default) | `after_sequence` integer, default 0 | State and latest changed frame |
| `input` | `input` object below | `editable` boolean |
| `asset` | `asset`: `browser.html`, `browser.js`, `browser.css` | `content_type`, UTF-8 `body` |

View returns `status`, `ready`, `done`, `expires_at`, `origin`, `viewport`, `sequence`
and optional `image={mime:"image/jpeg",base64:...}`. `viewport` is GrantBridge's own
`{width, height, scale}`, relayed untouched; the viewer sizes and sharpens its frames from
it. After the login has ended, view answers a fixed placeholder (`390×760`, `scale` 1).
Frames stay in memory; JPEGs are bounded to 512 KiB and RPC responses remain below 1 MiB.
View is short polling.

Inputs are `tap` with normalized `x,y`; `drag` with 2–128 normalized `points`;
`scroll` with `dy` within ±2000 and optional `x,y`; `text` up to 4096 characters; or
`key` with Enter, Backspace, Tab, Escape, ArrowLeft, ArrowRight, ArrowUp or ArrowDown.
Arbitrary navigation, scripts, paths and selectors are not accepted. Never retry input
automatically when its result is unknown. Recover state through `view` instead.

Serve the three fixed assets in one owned browser directory. The viewer calls relative
`GET view?after_sequence=N`, `POST input` and `POST cancel`. Map these to the browser
channel and existing `accounts.login.cancel`. The viewer sends JSON with
`X-GrantBridge-Browser: 1`. Authenticate and authorize every route, require same-origin
POSTs/CSRF protection, bound request bodies to 16 KiB, and disable caching and body logs.

Recommended headers: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
`X-Content-Type-Options: nosniff`. CSP allows scripts/styles/connect only from self,
images from self/data, same-origin frames, no base URI and no form submission.
The viewer sends a same-origin `grantbridge.browser.done` message to its parent.
This is a presentation signal; only the SDK's status/check/complete verifies an account.

## Server runtime and credentials

The wheel includes pinned Node, GrantBridge JavaScript, playwright-core and viewer
assets. The host supplies Chrome (`GRANTBRIDGE_CHROME`), Xvfb, xauth, their shared
libraries, fonts/fontconfig and CA certificates. Provide private writable `/tmp`,
`/run`, `/dev/shm` and normal `/proc` and `/dev`. No host DISPLAY is used.

Set `GRANTBRIDGE_BROWSER_PROXY=http://127.0.0.1:18080` for Fullbrain's egress proxy.
Chrome receives an explicit proxy argument, with loopback bypass for callback/CDP.
Chrome's own sandbox is enabled by default. A trusted host already isolating Chrome
in its worker sandbox may explicitly set `GRANTBRIDGE_BROWSER_SANDBOX=external`.
This adds `--no-sandbox`; there is no automatic fallback or arbitrary argument API.

The proxy adapter stores its temporary database and fresh browser profiles in a private
job directory. Terminal/cancel/expiry closes the browser and removes its profile;
shutdown removes the whole job directory. It never creates `store.root/grantbridge`.
Credential inventory admits only the verified bundled runtime and no custom data root.
Active login still prevents drained credential capture. No provider credentials move
out of CLIProxyAPI, and recovery never silently resumes consent.

## Compatibility and acceptance

Existing same-host, isolated-browser and callback APIs remain available for other SDK
consumers. Fullbrain should use the single hosted flow above, removing its replaced
manual URL/callback and host-desktop paths. For source connectors whose registered callback
is loopback, the full GrantBridge SDK accepts `source.mcp.start` with
`presentation: "embedded"` and exposes `source.mcp.browser` using the same packaged viewer.
The SDK observes the exact owner-and-attempt-bound callback inside its browser and completes
its existing OAuth flow. Source credentials remain in GrantBridge's existing vault and
snapshot custody. Genuine QR pairing and actually public callbacks retain their paths.

Deterministic tests cover ownership, idempotency, input, account verification, admission
and recovery. Browser/package tests use fixture provider pages. Real provider consent,
phone hardware and Fullbrain's production bwrap mounts require separate integration
acceptance; they are not claimed by the fixture tests.
