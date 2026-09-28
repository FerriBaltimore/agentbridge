# Changelog

All notable changes to AgentBridge are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). "Fixture tested" means
deterministic subprocess coverage; live provider acceptance is recorded separately in
`docs/development/*-acceptance.md` and `docs/interface/provider-acceptance.md`.

Release procedure: [docs/release.md](docs/release.md). Compatibility numbers and the
AgentBridge × GrantBridge × Fullbrain v2 matrix: [docs/compatibility.md](docs/compatibility.md).

## [Unreleased]

## [2.5.0] - 2026-09-28

First release cut with a manifest (`tools/release_manifest.py`) and a compatibility matrix.
Everything since the `2.3.3` package metadata is listed. The part Fullbrain v2 already runs
as its pinned artifact `a7ac24a5…` (v2 label `2.4.2`, AgentBridge commit `32e3cbd`) is
marked *(in the v2 pin)*; it was never published under its own version.

Compatibility: Python ≥ 3.11; Linux x86_64 and aarch64 `manylinux_2_28` wheels; GrantBridge
≥ `1.0.0-rc.1` when AgentBridge is pointed at an external GrantBridge checkout or artifact
(`--grantbridge-root`, `AGENTBRIDGE_GRANTBRIDGE_ROOT`), because the mobile login entry
needs the `auth.proxy_start` fields (`browser`, `mode`, `owner`, `viewerUrl`) that first
ship in that release. The wheel bundles its own copy of that adapter, so same-host,
isolated and mobile-browser logins work without an external GrantBridge; only
`mode: "hosted"` needs a long-lived GrantBridge host. AgentBridge does not use the source
broker (`source.*`, `client_credentials`, `local_session`). The Fullbrain v2 pin for this
version is pending (`docs/compatibility.md`).

### Added

- Mobile logins through GrantBridge (`14aa9da`): `accounts.login.start` accepts
  `browser: "mobile"` with `mode: "browser"` (the phone opens `authorization_url` and
  returns the loopback redirect through `accounts.login.callback` or GrantBridge's
  owner-bound `/oauth/proxy/callback`) or `mode: "hosted"` (the phone opens `viewer_url`
  on the GrantBridge origin). Every attempt projection echoes `browser` and `mode`;
  `owner_ref` binds the GrantBridge viewer and callback route. New pre-dispatch errors
  `hosted_browser_unavailable` and `invalid_browser`; a hosted login can end `failed` with
  `browser_busy`, `browser_closed` or `hosted_browser_unavailable`; a GrantBridge that does
  not honour the phone entry is cancelled and reported as `provider_protocol_error`.
  Contract: `docs/interface/mobile-login.md`.
- Isolated desktop login entry (`4bdc4d0`): `browser: "isolated"` keeps the same-host wire
  request while the host opens the URL in a fresh disposable Chromium profile through
  `agentbridge.auth_browser.IsolatedAuthBrowser` (stdlib only; `available()` is false
  without a display or Chromium). AgentBridge itself never opens a browser.
- `identity.email` on a login attempt that fails with `identity_changed`, so a host can
  name the observed and the expected domains. The credential is retired, never bound.
- CLI: `accounts login` and `accounts login-start` take `--browser
  same_host|isolated|mobile` and `--mode browser|hosted`.
- `agentbridge.protocol_versions`: the numbers a consumer compares (`rpc_contract`,
  `http`, `cli`, `login_start`) and the GrantBridge minimum, in one module.
- `tools/release_manifest.py` writes `dist/agentbridge-<version>.manifest.json` (wheel and
  sdist digests, git commit, Python range, protocols, GrantBridge minimum, bundled runtime
  versions, Fullbrain pin note, `built_at`) and validates it with `--check`; `--build`
  builds the sdist and wheel through the in-tree backend without network access.
- Reproducible sdists: `tools/bundle_backend.py` normalises the sdist (sorted members,
  owner 0, mtime `SOURCE_DATE_EPOCH`, zeroed gzip header) when `SOURCE_DATE_EPOCH` is set.
  Wheels already honour that variable.
- Docs: `docs/interface/fullbrain-v2.md` (the surface Fullbrain v2 uses, with the version
  each shape appeared in), `docs/compatibility.md` and `docs/release.md`.
- *(in the v2 pin)* Persistent message queues (`73a9c2e`): `queues.list`, `queues.add`,
  `queues.move`, `queues.delete`, `queues.dispatch`, `queues.pause`, `queues.resume`,
  `messages.get`, `messages.create(delivery="queue"|"steer"|"interrupt")`, durable
  `queue.changed` events, a detached dispatcher and the `agentbridge queues` CLI.
- *(in the v2 pin)* Durable `permission_mode` and `sandbox_mode` instance defaults with
  per-turn overrides, `instances.delete`, automatic account affinity
  (`affinity_account_ref`), native namespace isolation for full access and cache
  observations (`73a9c2e`).
- *(in the v2 pin)* Context package limits (`32e3cbd`): `MAX_INSTRUCTION_BYTES` 1 MiB and
  `MAX_PACKAGE_BYTES` 1.5 MiB; upstream provider selection inside context packages.

### Changed

- Bundled GrantBridge proxy adapter refreshed to `ddaa3698…` (`agentbridge-phone-callback`);
  `src/agentbridge/bundle/lock.json` follows.
- `capabilities.get` login limitations now read `same_host_isolated_or_mobile_browser` and
  `hosted_browser_requires_grantbridge_host`.
- Identity checks and login entry rules moved to `auth_identity.py` and `auth_entry.py`;
  `auth_browser.py` moved from the playground into the package.
- *(in the v2 pin)* `interactive_worker` reports a non-Codex engine as a `launch` failure
  with outcome `not_started` (`32e3cbd`).

## [2.3.3] - 2026-09-25

- `accounts.pause` and `accounts.resume`: a durable local routing choice that keeps the
  login, quota observations, history and admitted turns; playground flows refined.

## [2.3.2] - 2026-09-25

- Live account quota windows: `accounts.usage(refresh=true)` performs one bounded upstream
  quota read through the local proxy and reports `quota_windows[]` with `stale_at` and
  `refresh_reason`; a failed refresh keeps the previous observation and its age.

## [2.3.1] - 2026-09-25

- Observed context choices in `models.list` (`reasoning_efforts`, `context_windows`,
  `input_modalities`, per-account capabilities) and the normalized turn event stream
  (`Bridge.turn_events_stream`, bounded long polling on `turns.events`).

## [2.3.0] - 2026-09-24

- Playground 2.3 and account recovery: `accounts.login.list` returns interrupted attempts
  so a trusted host can cancel or resume one whose start response was lost.

## [2.2.0] - 2026-09-24

- Compact playground; route changes between turns through `instances.update` while the
  instance keeps its immutable native Codex session.

## [2.1.0] - 2026-09-24

- Bundled Linux runtimes: one `manylinux_2_28` wheel per architecture with pinned
  CLIProxyAPI, Codex, GrantBridge proxy adapter and Node.js (`bundle/lock.json`);
  installed wheels never download runtimes.

## [2.0.0] - 2026-09-23

- AgentBridge v2: Codex as the sole execution engine through a dedicated local CLIProxyAPI
  sidecar per account, GrantBridge-coordinated `accounts.login.*` onboarding,
  `accounts.login.callback` for remote redirects, automatic and pinned model-first
  routing, `capabilities.get` with `contract_version: "v2"`, evaluation instances,
  bounded `context_package` and private `mcp`, and the local playground.

Versions before 2.0.0 (`0.1.0`) were direct Codex and Claude Code adapters; their records
stay readable but cannot create accounts or start turns.

[Unreleased]: https://github.com/FerriBaltimore/agentbridge/compare/v2.5.0...HEAD
[2.5.0]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.5.0
