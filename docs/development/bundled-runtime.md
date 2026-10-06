# Bundled Linux runtime

AgentBridge delivers one platform wheel per Linux architecture: x86_64 and
ARM64 (aarch64). Python 3.11 or newer and a compatible Linux host are still
required. The wheel contains the components used by the one Codex-through-
CLIProxyAPI execution and GrantBridge account-login flow:

| Component | Pinned version | Wheel resource |
| --- | --- | --- |
| CLIProxyAPI | 7.3.16 `no-plugin` release | Release archive and its MIT license |
| Codex CLI | 0.153.0 | Official package archive, license and notice |
| GrantBridge | 1.0.0-rc.3, exact source-broker artifact closure | Static proxy modules and license |
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

## Deriving GrantBridge from its release

The wheel's dependency-free proxy adapter comes from the same verified release
tarball as the independent source broker. Never edit the bundled modules by hand.
After verifying the upstream manifest and archive signature using the host's
existing trusted signer, pass that trusted archive digest to the importer:

```sh
python tools/import_grantbridge.py /private/grantbridge-VERSION.tgz \
  /private/grantbridge-VERSION.manifest.json \
  --expected-sha256 TRUSTED_ARCHIVE_SHA256 --write
python tools/verify_bundle_wheel.py dist/wheels/SELECTED.whl \
  --grantbridge-archive /private/grantbridge-VERSION.tgz \
  --grantbridge-manifest /private/grantbridge-VERSION.manifest.json
```

The importer checks every archive member against the manifest, parses static
ESM imports without executing them and rejects external or dynamic dependencies.
The generated lock records the source version, commit, complete archive digest
and exact closure digest. Wheel verification compares packaged bytes with that
closure; the full npm dependency tree is not duplicated in the wheel.

## Installed behavior

The installed wheel runs without downloading runtime components or installing
Codex, CLIProxyAPI, GrantBridge or Node separately. On first use, AgentBridge
checks the packaged hashes and extracts required files into a versioned,
private `bundled-runtimes` directory under its state root. The default root is
`${XDG_STATE_HOME:-~/.local/state}/agentbridge`. The runtime does not select
those executables from `PATH` in the normal managed flow.

Keep the wheel installation and private state root outside any project
workspace that Codex can modify. Managed sidecars also need Linux `memfd`,
loopback communication and provider network access. The sidecar starts with
`-local-model` and its panel updates disabled, so it uses its embedded model
catalogues and makes no remote catalogue or panel fetches of its own; the
current pinned build still checks one remote version manifest at start, which an
egress policy may deny without affecting login. A host sandbox must allow
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
