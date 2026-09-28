# Compatibility

AgentBridge is consumed as a versioned Python package: `ferran-agentbridge`, tag `vX.Y.Z`,
one `manylinux_2_28` wheel per architecture, an sdist and a signed
`dist/agentbridge-X.Y.Z.manifest.json` ([release.md](release.md)). This document says which
numbers a consumer checks, how each may move, and which AgentBridge × GrantBridge ×
Fullbrain v2 triples are known to work. The machine-readable copy of the numbers is
`agentbridge.protocol_versions`; `tools/release_manifest.py` copies it into the manifest.

| Number | Current | Where it appears |
| --- | --- | --- |
| SDK version | `2.5.0` | `pyproject.toml`, `__version__`, `--version`, tag, manifest |
| `rpc_contract` | `v2` | manifest `protocols`; `capabilities.get` `contract_version` |
| `http` | `2` | manifest `protocols`; playground `/api/meta` `api_revision` |
| `cli` | `1` | manifest `protocols`; `agentbridge --root DIR rpc`, `--version` |
| `login_start` | `2` | manifest `protocols`; `accounts.login.start` shape |
| `grantbridge_min` | `1.0.0-rc.1` | manifest |
| `python_requires` | `>=3.11` | `pyproject.toml`, manifest |
| `platform` | `linux_x86_64`, `linux_aarch64` | wheel tag `manylinux_2_28_<arch>`, manifest |
| `bundle` | CLIProxyAPI 7.3.16, Codex 0.153.0, Node v24.21.0 | `bundle/lock.json`, manifest |

The bundled GrantBridge proxy adapter is commit `ddaa3698…` (`bundle.grantbridge_commit`);
the x86_64 bundle also locks the `native_bwrap` 0.11.1.roproc1 launcher of the host-isolated
profile (`bundle/lock.json` `native_bwrap`, archive and executable digests).
`tools/verify_bundle_wheel.py` checks every bundled asset of a built wheel against the lock.

## SDK version

Semantic Versioning over the whole package: the Python SDK, the CLI, the JSON-RPC stdio
surface, the bundled runtimes and these documents move together under one `vX.Y.Z` tag.

- **Major**: an incompatible change — a new `rpc_contract` identifier, a removed or renamed
  method or field, a changed field type, a request key made required, a narrowed Python
  range, a Python SDK signature break.
- **Minor**: additive — new methods, optional request keys, response fields, error codes,
  event kinds, `capabilities.get` operations or limitations, a new `login_start` number,
  a new bundled runtime version, a wider platform list.
- **Patch**: behaviour fixes with unchanged shapes and unchanged bundled runtimes.

The package metadata is the only version. Fullbrain v2 labelled its pinned artifact
`a7ac24a5…` as `2.4.2` although the AgentBridge checkout it was built from carried `2.3.3`
in `pyproject.toml`; no AgentBridge commit ever carried a `2.4.x` version. From `2.5.0` on,
the version a consumer records is the manifest `version`, nothing else.

## Protocol numbers

- `rpc_contract` — the `capabilities.get` result carries `contract_version`. Fullbrain v2
  compares it with exact equality and refuses another value before any other call. Inside
  `v2`, additive changes (methods, optional keys, fields, codes, kinds) happen in minor
  releases; a consumer must map unknown error codes to its generic failure and treat
  unknown login states and event kinds conservatively. Anything breaking becomes `v3` and
  a major release.
- `http` — the playground's own HTTP API, not used by Fullbrain v2. The page sends the
  revision it was served with; a mismatch answers `playground_update_required`. It changes
  whenever a request or response field the page depends on changes.
- `cli` — the command line a host invokes: `agentbridge --root DIR rpc`, `--version` and
  the `accounts login*` commands. Options are only added inside `1`; a removed or renamed
  command, option or output shape becomes `2` and a major release.
- `login_start` — the `accounts.login.start` request and the attempt projection returned
  by `start`, `status`, `check`, `complete.attempt` and `cancel`. `1` (2.0.0): same-host
  login only. `2` (2.5.0): `browser` `same_host|isolated|mobile`, `mode` `browser|hosted`,
  `viewer_url` for hosted logins, `browser` and `mode` echoed on every projection,
  `identity.email` on `identity_changed`. A consumer that needs a phone entry requires
  `login_start >= 2`; a consumer that sends only the 2.0.0 fields works with either.

## GrantBridge minimum

`grantbridge_min` is the oldest GrantBridge release whose engine surface — the
`auth.proxy_start`, `auth.proxy_status`, `auth.proxy_cancel`, `auth.proxy_callback` and
`auth.close` methods of `scripts/agentbridge-proxy-adapter.mjs` — serves every login entry
of this AgentBridge when a host points it at an external GrantBridge (`--grantbridge-root`,
`AGENTBRIDGE_GRANTBRIDGE_ROOT`, or a hosted GrantBridge for `mode: "hosted"`).

For `2.5.0` it is `1.0.0-rc.1`, the first GrantBridge release. The reason is the mobile
entry, not the source broker: `browser: "mobile"` sends `browser`, `mode` and `owner` in
`auth.proxy_start` and needs `browser`/`mode` echoed and `viewerUrl` for hosted logins.
Those fields exist from GrantBridge commit `ddaa3698…` (`agentbridge-phone-callback`), which
is an ancestor of the `1.0.0-rc.1` manifest commit `95f5307…`; no earlier GrantBridge was
ever released (`package.json` `0.1.0`, untagged). A GrantBridge without that entry makes a
mobile start fail with `provider_protocol_error` after AgentBridge cancels the sidecar
session; `same_host` and `isolated` logins send the unchanged 2.0.0 wire request and work
with any GrantBridge that exposes the `0.1.0` engine surface (`health` →
`{service: "grantbridge", version: "0.1.0"}`), including the unreleased `main`.

AgentBridge does not call the source broker (`source.*`, `client_credentials`,
`local_session`, `source.mcp.session.open`); those are consumed by Fullbrain v2 directly
and impose no AgentBridge requirement. The wheel bundles its own copy of the adapter at
`ddaa3698…`, so an installation without an external GrantBridge already satisfies the
minimum for `same_host`, `isolated` and `mobile` + `mode: "browser"`; only `mode: "hosted"`
needs a long-lived GrantBridge host, which is where `grantbridge_min` is enforced.

GrantBridge states its own bound in its manifest: `agentbridge_min` `2.4.2`, the label of
the artifact Fullbrain v2 pins today. `2.5.0` satisfies it. Both bounds must hold.

## Matrix

| AgentBridge | Commit | GrantBridge minimum | Fullbrain v2 pin (`PIN_SHA256`) | v2 label |
| --- | --- | --- | --- | --- |
| `2.3.3` | `32e3cbd` + working tree | none released (see below) | `a7ac24a5…` | `2.4.2` |
| `2.5.0` | tag `v2.5.0` | `1.0.0-rc.1` | `3786cea5…` | `2.5.0` |

- `2.3.3` row (history): the previous v2 pin
  `a7ac24a5254711cf0e253cff653f5c77fec5b15c2986a59c98b99174e73d7b47`, built from a reviewed
  working tree on `73a9c2e` whose committed part became `32e3cbd` (P05) and whose
  uncommitted part (`native_bwrap`, host-isolated access, notices, verified identity) is
  commit `3b406bd` of this branch. Its login entry is same-host only, so it needs no
  released GrantBridge: the engine surface of GrantBridge `main` `d60873c` (`0.1.0`,
  untagged) suffices.
- `2.5.0` row: the current v2 pin
  `3786cea5b8d637ef3691284c9f5d2b9088663dcdde27bece6f6e8c8e422bc4c8` (`known-versions.json`
  `components.agentbridge.latest_known`, 149 source files, 182,210,560 bytes, manifest
  `5ec10fa7df0f4c1d309f539b6debdf6bfc122fe9ef3163f9c4c50c077ad36957`), built with
  `dev/agentbridge/build_v2.py` from this branch with its x86_64 bundle prepared. The
  commit is the manifest `git_commit`, which must equal `git rev-parse v2.5.0^{commit}` once
  the owner tags.

The pin column is not the wheel digest: Fullbrain v2 pins the SHA-256 of the archive that
`dev/agentbridge/build_v2.py` builds from the AgentBridge checkout with its x86_64 bundle
prepared (`fullbrain_pin_note` in the manifest). The same digest goes to
`backend/fullbrain/adapters/agentbridge/client.py`, `tests/fixtures/agentbridge_protocol.json`
(`immutable_artifact_sha256`, `sdk_version`, `base_commit`) and
`deploy/delivery/known-versions.json` (`components.agentbridge`). The v2 builder also
requires the `native_bwrap` asset and license that `bundle/lock.json` locks since `3b406bd`;
`tools/prepare_bundle.py` places both next to the other runtimes.

## The rule the Fullbrain v2 installer applies

Before a worker starts, the installer reads the pinned AgentBridge manifest
(`pins/agentbridge.json` → `agentbridge-X.Y.Z.manifest.json`) and the pinned GrantBridge
manifest (`pins/grantbridge.json` → `grantbridge-X.Y.Z.manifest.json`), verifies both
signatures and digests, and then:

1. Requires `agentbridge.grantbridge_min <= grantbridge.version` and
   `grantbridge.agentbridge_min <= agentbridge.version`, comparing Semantic Versions with
   pre-release ordering (`1.0.0-rc.1 < 1.0.0`).
2. Requires the pair `(agentbridge.version, grantbridge.version)` to fall inside the rows
   of this matrix: the AgentBridge row must exist and the GrantBridge version must be at
   least the row's minimum. A version with no row, or a GrantBridge below the row's
   minimum, is rejected before any process starts, with both versions in the message.
3. Requires `agentbridge.protocols.rpc_contract == "v2"` and, when the phone login entry is
   enabled in v2, `agentbridge.protocols.login_start >= 2`.
4. Requires the installed archive digest to equal `PIN_SHA256` (unchanged check), and after
   start requires `capabilities.get.contract_version == "v2"` (unchanged check).

A rejected pair leaves the previous installation untouched. `make pins-update VERSION=…`
rewrites the pin from a published manifest; it does not bypass these checks.
