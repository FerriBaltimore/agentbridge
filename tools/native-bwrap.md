# Reviewed native bubblewrap launcher

`native_bwrap` version `0.11.1.roproc1` adds an explicit `--proc-ro DEST`
operation to bubblewrap 0.11.1. The patch mounts a new procfs read-only from
its initial mount and requires a private PID namespace. Existing `--proc`
behavior is unchanged. No installed system executable or host policy is modified.

The host-isolated execution profile uses an ordinary outer `/proc` for namespace
setup and an additional pristine read-only procfs visible to the kernel. The inner
namespace mounts its own read-only `/proc`; it does not bind an ancestor procfs
or expose the outer helper mount. This supports kernels that require an existing
fully visible procfs before allowing another mount in a nested user namespace.
The caller still needs the host's permission to create user namespaces.

## Source and reproducible build

The exact upstream commit, dependency package versions and SHA-256 values are in
[native_bwrap_provenance.json](native_bwrap_provenance.json). The source is
[bubblewrap v0.11.1](https://github.com/containers/bubblewrap/tree/124c4cdf4321f63ef17a1cb0ce8f9dd45bd7adbe).
Apply [native_bwrap.patch](native_bwrap.patch) only to that commit.

On Linux x86-64 with the recorded Ubuntu 26.04 toolchain, run:

```sh
bash tools/native_bwrap_build.sh /path/to/new/private/build-directory
```

The recipe clones upstream source, downloads and verifies the two pinned libcap
packages, extracts them locally, and creates a private Meson environment. It does
not install system packages. GCC, binutils, glibc development files, Ninja,
pkgconf, Git, Python, uv, apt and dpkg-deb must already be available. The exact
compiler and system library versions matter for byte-for-byte reproduction.

The script retains the patched source, dependency archives, extracted library
files, compiled objects and Ninja build commands in the supplied directory.
It verifies the resulting static binary and deterministic archive against the
SDK lock. Three independently configured static builds, including one from a
fresh clone, produced the recorded binary SHA-256. Repacking the archive with
the retained notices also reproduced its recorded SHA-256.

Copy the validated `native_bwrap.tar.gz` to
`src/agentbridge/bundle/assets/native_bwrap.tar.gz` before building a wheel.
`prepare_bundle.py` accepts this local resource or its verified build cache;
there is no published download URL for the patched launcher. The archive contains
only `bin/bwrap` and `LICENSE`, with fixed modes, owner fields and timestamps.
The upstream Codex archive remains unchanged.

## Static dependencies and relinking

The executable statically links libcap 2.75 and glibc 2.43. Their exact Ubuntu
source packages and revisions are linked in the provenance record. The archive's
`LICENSE` retains bubblewrap's LGPL terms, libcap's copyright and license notices,
glibc's copyright and license notices, and the LGPL 2.1 text. Those third-party
terms do not select a license for AgentBridge itself.

To modify or relink the launcher, obtain the recorded upstream and dependency
sources, apply the retained patch, and build the desired libraries. Point
`PKG_CONFIG_SYSROOT_DIR`, `PKG_CONFIG_LIBDIR` and `LIBRARY_PATH` at the replacement
libcap installation; use a toolchain/sysroot containing the replacement glibc.
Reconfigure the retained source with the recipe's Meson options and run Ninja.
`ninja -C build -t commands` exposes the compile and link commands, and
`build/bwrap.p` retains the launcher objects for relinking. A changed executable
will intentionally fail the original lock check; review and record its new
binary, archive and dependency hashes before distributing it as another build.

## Evidence and platform limits

The reviewed binary is statically linked x86-64 ELF. A host experiment using the
existing user-namespace permission successfully created the nested private PID
namespace and fresh read-only procfs: `/proc/self/exe` referred to the real inner
Python process, and the outer helper procfs was absent. The runtime integration
also checked ordinary processes and threads with its post-setup seccomp policy.
This does not establish compatibility with every Linux kernel or host policy.

No ARM launcher has been built or validated. ARM wheels retain the standard SDK
runtimes; the host-isolated profile reports unavailable capability when the
reviewed launcher or required host isolation is unavailable.
