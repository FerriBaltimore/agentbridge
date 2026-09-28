# Release procedure

An AgentBridge release is a git tag `vX.Y.Z`, one `manylinux_2_28` wheel per delivered
architecture, an sdist, `dist/agentbridge-X.Y.Z.manifest.json` describing them, a detached
OpenSSH signature over the manifest, and a GitHub Release holding those files. Agents prepare
everything up to and including the build and the manifest; **tagging, signing and publishing
are owner actions** because they need the release key and the GitHub account. Nothing here
needs `sudo`, Docker or a network connection until the upload, except a cold bundle cache
(`dist/bundle-cache`, see [bundled-runtime.md](development/bundled-runtime.md)).

The example below cuts `v2.5.0`; replace the version for later releases.

## What is published

| File | Content |
| --- | --- |
| `ferran_agentbridge-2.5.0-py3-none-manylinux_2_28_x86_64.whl` | SDK, CLI, RPC, x86_64 runtimes |
| `ferran_agentbridge-2.5.0-py3-none-manylinux_2_28_aarch64.whl` | same for ARM64, when delivered |
| `ferran_agentbridge-2.5.0.tar.gz` | sdist: sources, `tools/`, the build host's prepared bundle |
| `agentbridge-2.5.0.manifest.json` | what a consumer pins and the owner signs |
| `agentbridge-2.5.0.manifest.json.sig` | `ssh-keygen -Y sign`, namespace `agentbridge-release` |

Manifest fields (`tools/release_manifest.py`, checked by `tests/test_release_manifest.py`):

| Field | Meaning |
| --- | --- |
| `schema` | `1` |
| `name`, `version` | `ferran-agentbridge`, the `pyproject.toml` version; `__version__` must match |
| `git_commit`, `working_tree_dirty` | the built checkout; a release build must be clean |
| `python_requires` | `pyproject.toml` `requires-python` |
| `platform` | `linux_x86_64` or `linux_aarch64`, the wheel described by `wheel` |
| `wheel`, `sdist` | `{file, sha256, bytes}` of the files beside the manifest |
| `protocols` | `rpc_contract`, `http`, `cli`, `login_start` from `agentbridge.protocol_versions` |
| `grantbridge_min` | oldest compatible GrantBridge release ([compatibility.md](compatibility.md)) |
| `bundle` | CLIProxyAPI, Codex and Node versions and the GrantBridge adapter commit from the lock |
| `fullbrain_pin_note` | how Fullbrain v2 derives `PIN_SHA256` from this checkout |
| `built_at`, `source_date_epoch` | UTC time; `SOURCE_DATE_EPOCH` when set, so rebuilds match |

The wheel and the sdist are reproducible: with the same checkout, interpreter, setuptools
and `SOURCE_DATE_EPOCH`, two builds give byte-identical files. The wheel takes its zip
timestamps from the variable; the build backend rewrites the sdist with sorted members,
owner 0, that mtime and a zeroed gzip header (`tools/reproducible_sdist.py`).

## One-time owner steps

1. Release key, outside every repository, never on servers or agent machines:

   ```sh
   ssh-keygen -t ed25519 -C releases@agentbridge -f ~/.ssh/agentbridge-release
   chmod 600 ~/.ssh/agentbridge-release
   ```

2. Trust list for consumers, public half only, kept by Fullbrain v2 next to its pin:

   ```sh
   printf 'releases@agentbridge namespaces="agentbridge-release" %s\n' \
     "$(cut -d' ' -f1,2 ~/.ssh/agentbridge-release.pub)" > allowed_signers
   ```

   Principal `releases@agentbridge` and namespace `agentbridge-release` are fixed.
3. `gh auth login`, or a fine-grained token with *Contents: read and write* on
   `FerriBaltimore/agentbridge`, kept in the shell only.

## Cutting v2.5.0 (owner)

Start from the reviewed branch with a clean tree (`git status --porcelain` prints nothing).

1. **Version and changelog** (an agent may prepare this commit): `pyproject.toml` and
   `src/agentbridge/__init__.py` say `2.5.0`; `CHANGELOG.md` has `## [2.5.0] - YYYY-MM-DD`
   with a fresh empty `[Unreleased]` above it and updated link references. Check that
   `.venv/bin/agentbridge --version` prints `2.5.0` (the editable install reads the source;
   `pip list` shows the new version only after the wheel is reinstalled in step 6).
2. **Repository check**: `python3 tools/check_repository.py` (also run by the pre-commit hook
   on staged blobs).
3. **Deterministic tests**: the full suite is the documented gate and finishes in a few
   minutes on the reference host: `python3 -m pytest`. Tests use fixtures and local synthetic
   providers only; they never discover or use real accounts. Skips report missing optional
   host features (display, `bwrap`, Landlock) and are acceptable; a failure is not.
4. **Build and manifest**, with the interpreter that has setuptools ≥ 70.1 (the build backend
   is in-tree; no network is needed for build dependencies):

   ```sh
   export SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)
   python3 tools/release_manifest.py --build            # sdist + x86_64 wheel + manifest
   python3 tools/verify_bundle_wheel.py dist/*manylinux_2_28*.whl
   python3 tools/release_manifest.py --check
   ```

   `python -m build --no-isolation` followed by `python3 tools/release_manifest.py` is
   equivalent. For ARM64 add `AGENTBRIDGE_TARGET_ARCH=aarch64 python3 tools/release_manifest.py
   --build --arch aarch64 --output dist/agentbridge-2.5.0-aarch64.manifest.json`; Fullbrain v2
   pins x86_64 only.
5. **Reproducibility**: build a second time into another directory with the same
   `SOURCE_DATE_EPOCH` and compare `sha256sum`; the wheel and the sdist must match. The manifest
   also records `working_tree_dirty: false` and the `git_commit` you are about to tag.
6. **Disposable installation**: `python3 -m venv /tmp/ab-smoke && /tmp/ab-smoke/bin/pip install
   --no-index --no-deps dist/*.whl`, then `/tmp/ab-smoke/bin/agentbridge --version`,
   `/tmp/ab-smoke/bin/python examples/installed_quickstart.py` and
   `/tmp/ab-smoke/bin/agentbridge --root "$(mktemp -d)" models list --json` (an empty
   catalogue). Reinstall the wheel into this repository's `.venv` as AGENTS.md asks.
7. **Tag**: `git tag -a v2.5.0 -m "AgentBridge v2.5.0"` (add `-s` if you sign tags). Confirm
   `git rev-parse v2.5.0^{commit}` equals the manifest `git_commit`.
8. **Sign** the manifest (it carries the wheel and sdist digests, so one signature covers
   the release):

   ```sh
   ssh-keygen -Y sign -f ~/.ssh/agentbridge-release -n agentbridge-release \
     dist/agentbridge-2.5.0.manifest.json
   ssh-keygen -Y verify -f allowed_signers -I releases@agentbridge -n agentbridge-release \
     -s dist/agentbridge-2.5.0.manifest.json.sig < dist/agentbridge-2.5.0.manifest.json
   ```

9. **Publish**: push the branch and the tag, then create the GitHub Release with the wheel(s),
   the sdist, the manifest and its signature, using the changelog section as notes:

   ```sh
   git push origin HEAD v2.5.0
   gh release create v2.5.0 dist/ferran_agentbridge-2.5.0-*.whl \
     dist/ferran_agentbridge-2.5.0.tar.gz dist/agentbridge-2.5.0.manifest.json \
     dist/agentbridge-2.5.0.manifest.json.sig --title "AgentBridge v2.5.0" \
     --notes-file <(awk '/^## \[2.5.0\]/,/^## \[2.3.3\]/' CHANGELOG.md | sed '$d')
   ```

   Asset URLs follow
   `https://github.com/FerriBaltimore/agentbridge/releases/download/v2.5.0/<asset>`.
   Publishing is per tag: never replace an asset under an existing tag; cut `2.5.1`.

## After publishing: the Fullbrain v2 pin (v2 lane)

Fullbrain v2 pins the digest of the archive that `dev/agentbridge/build_v2.py` builds, not
the wheel digest ([compatibility.md](compatibility.md) § Matrix). From the tagged checkout with
its x86_64 bundle prepared (`python3 tools/prepare_bundle.py`, or after the wheel build):

1. `python3 dev/agentbridge/build_v2.py <agentbridge checkout> <artifacts dir>` prints the
   archive `sha256` and `manifest_sha256`. The builder also expects the `native_bwrap` asset
   and license of the v2 native launcher, which this repository's lock does not carry; the v2
   lane reconciles that first.
2. Set `PIN_SHA256` in `backend/fullbrain/adapters/agentbridge/client.py` to the archive
   digest.
3. Refresh `tests/fixtures/agentbridge_protocol.json`: `base_commit` (= `git_commit`),
   `working_tree_dirty: false`, `immutable_artifact_sha256`, `artifact_bytes`,
   `artifact_source_files`, `manifest_sha256`, `sdk_version: "2.5.0"`, the `files[]` digests
   of `authentication.py`, `auth_contract.py`, `auth_entry.py`, `auth_identity.py`,
   `auth_browser.py`, `grantbridge.py`, `capabilities.py`, `commands/parser.py`,
   `bundle/lock.json` and the bundled GrantBridge copies; operations are unchanged.
4. Set `components.agentbridge` in `deploy/delivery/known-versions.json` to the archive digest
   with label `2.5.0`, and write `pins/agentbridge.json` (`version`, `url`, `sha256`,
   `manifest_sha256`, `signature`) from the published manifest and `.sig`.
5. Apply the installer rule of [compatibility.md](compatibility.md) against the pinned
   GrantBridge manifest, then run the v2 bridge contracts and the login journeys.

## Rehearsal without the owner key

Use a throwaway key so no trust list is touched:

```sh
K=$(mktemp -d) && chmod 700 "$K"
ssh-keygen -q -t ed25519 -N '' -C releases@agentbridge -f "$K/key"
printf 'releases@agentbridge namespaces="agentbridge-release" %s\n' \
  "$(cut -d' ' -f1,2 "$K/key.pub")" > "$K/allowed_signers"
ssh-keygen -Y sign -f "$K/key" -n agentbridge-release dist/agentbridge-2.5.0.manifest.json
ssh-keygen -Y verify -f "$K/allowed_signers" -I releases@agentbridge -n agentbridge-release \
  -s dist/agentbridge-2.5.0.manifest.json.sig < dist/agentbridge-2.5.0.manifest.json
rm -rf "$K" dist/*.sig
```

## Rotation and revocation

Generate a new key, add its line to the consumers' `allowed_signers` beside the old one,
publish one release signed with the new key, then remove the old line. To revoke, remove the
line, bump the version and publish; consumers that updated reject anything signed only by the
removed key.
