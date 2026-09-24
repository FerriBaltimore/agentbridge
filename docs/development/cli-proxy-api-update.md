# Updating the bundled CLIProxyAPI candidate

AgentBridge pins one exact CLIProxyAPI release for Linux x86_64 and ARM64 in
`src/agentbridge/bundle/lock.json`. The release archives are platform specific.
They contain the upstream MIT license, which must accompany redistributed
binaries. The selected `no-plugin` builds match AgentBridge's disabled plugin
configuration and avoid a dynamic plugin loader.

Run the updater with an exact upstream version:

```sh
python tools/update_cliproxy.py --version v7.3.16
```

The command reads the release's `checksums.txt`, downloads both archives into
`dist/cli_proxy_api/v<version>/`, verifies their SHA-256, inspects safe archive
members, checks the ELF architecture and prints a candidate. It does not change
the lock unless `--write` is passed:

```sh
python tools/update_cliproxy.py --version v7.3.16 --write
```

Review the exact tag, release contents, upstream changes, checksums and the
candidate diff before delivering a new wheel. The upstream checksum file comes
from the same release, so it is a transfer check rather than independent proof
of authenticity. The committed lock pins exact bytes for later builds. There
is no runtime download or automatic update. The updater preserves lock entries
for Codex, GrantBridge and Node.

Build and test a new AgentBridge release for each supported Linux architecture.
Check the wheel's platform tag and installed binary hashes. Run the repository
guard, deterministic tests, disposable wheel install and installed SDK example.
Existing sidecars can still run an older binary after a wheel upgrade; report
their actual version and drain active work before explicitly restarting them.
There is currently no public in-place hot-restart operation for a managed
account. See the [bundled runtime guide](bundled-runtime.md) for the current
account lifecycle limit. Provider login, refresh, model routing and inference
need separate live acceptance with real accounts.
