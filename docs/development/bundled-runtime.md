# Bundled Linux runtime

AgentBridge delivers one platform wheel per Linux architecture: x86_64 and
ARM64 (aarch64). Python 3.11 or newer and a compatible Linux host are still
required. The wheel contains the components used by the one Codex-through-
CLIProxyAPI execution and GrantBridge account-login flow:

| Component | Pinned version | Wheel resource |
| --- | --- | --- |
| CLIProxyAPI | 7.3.16 `no-plugin` release | Release archive and its MIT license |
| Codex CLI | 0.153.0 | Official package archive, license and notice |
| GrantBridge | Proxy-only adapter at `d60873c999b7ee5215eb8cfa7bb65326175ff2f6` | Reviewed source files and license |
| Node.js | 24.21.0 | Official Linux archive and license |

The exact asset URLs and SHA-256 hashes are in
[`bundle/lock.json`](../../src/agentbridge/bundle/lock.json). AgentBridge's own
repository has no chosen public license; third-party notices do not license
AgentBridge for public distribution.

## Build a platform wheel

From a Linux source checkout, build the architecture you intend to deliver:

```sh
AGENTBRIDGE_TARGET_ARCH=x86_64 python -m pip wheel . --no-deps -w dist/wheels
AGENTBRIDGE_TARGET_ARCH=aarch64 python -m pip wheel . --no-deps -w dist/wheels
python tools/verify_bundle_wheel.py dist/wheels/*manylinux_2_28*.whl
```

The build backend serializes simultaneous wheel builds because both platform
builds stage archives in the same source tree. Run source-tree tests after the
x86_64 build so their local runtime assets match the host architecture.

The build backend fetches any missing archives into `dist/bundle-cache`, checks
each archive against the committed hash, validates its contents and ELF
architecture, and packages only the selected architecture. Building a source
checkout needs network access on a cold cache. A checksum from an upstream
release is a transfer check; review the upstream release and lock before
building. The resulting wheels use Linux architecture tags, so each must be
installed on its matching platform. Execute the installed smoke checks on
both platforms before claiming support for both.

## Installed behavior

The installed wheel runs without downloading runtime components or installing
Codex, CLIProxyAPI, GrantBridge or Node separately. On first use, AgentBridge
checks the packaged hashes and extracts required files into a versioned,
private `bundled-runtimes` directory under its state root. The default root is
`${XDG_STATE_HOME:-~/.local/state}/agentbridge`. The runtime does not select
those executables from `PATH` in the normal managed flow.

Keep the wheel installation and private state root outside any project
workspace that Codex can modify. Managed sidecars also need Linux `memfd`,
loopback communication and provider network access. A host sandbox must allow
the bundled files and private state root. Overrides such as
`AGENTBRIDGE_CLIPROXY_BIN`, `AGENTBRIDGE_GRANTBRIDGE_ROOT` or
`AGENTBRIDGE_NODE` are advanced development or integration choices; their
external contents are outside the reviewed wheel pin.

## Updating CLIProxyAPI

Use the [CLIProxyAPI update procedure](cli-proxy-api-update.md) to inspect an
exact upstream release and deliberately update its lock. Build, install and
verify new wheels for both architectures. There is no runtime download or
automatic update. Replacing a wheel does not replace an already-running
sidecar. Drain active work and explicitly stop and restart affected sidecars,
then verify their actual executable version. AgentBridge currently exposes no
public in-place sidecar hot-restart operation; retiring an account and logging
in again creates a new binding and requires authorization again.

Deterministic tests and installed-wheel smoke checks establish local packaging
and process behavior. They do not prove live OAuth, credential refresh, model
entitlement, quota accuracy or inference through any upstream provider. The
[bundle acceptance record](bundle-acceptance.md) gives the tested platforms and
artifacts. The
[provider acceptance gate](../interface/provider-acceptance.md) describes the
separate evidence needed for those claims.
