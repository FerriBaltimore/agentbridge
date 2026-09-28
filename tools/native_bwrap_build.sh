#!/usr/bin/env bash
# Reproduce with the Ubuntu 26.04 x86-64 toolchain pinned in native_bwrap_provenance.json.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
build_root=${1:?Usage: native_bwrap_build.sh NEW_PRIVATE_BUILD_DIRECTORY}
if [[ $(uname -m) != x86_64 ]]; then
  echo 'The reviewed native launcher is available only for Linux x86-64.' >&2
  exit 1
fi
mkdir -p "$build_root"
cd "$build_root"
git clone https://github.com/containers/bubblewrap.git source
git -C source checkout --detach 124c4cdf4321f63ef17a1cb0ce8f9dd45bd7adbe
git -C source apply "$repo_root/tools/native_bwrap.patch"
mkdir -p deps/root
(cd deps && apt download libcap-dev=1:2.75-10ubuntu2 libcap2=1:2.75-10ubuntu2)
python3 - "$repo_root" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

record = json.loads((Path(sys.argv[1]) / 'tools/native_bwrap_provenance.json').read_text())
for name, expected in record['dependencies'].items():
    actual = hashlib.sha256((Path('deps') / name).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit('The dependency archive differs from its pinned SHA-256.')
PY
for package in deps/*.deb; do
  dpkg-deb --extract "$package" deps/root
done
uv venv build-tools
uv pip install --python build-tools/bin/python meson==1.9.2
export PKG_CONFIG_SYSROOT_DIR="$PWD/deps/root"
export PKG_CONFIG_LIBDIR="$PWD/deps/root/usr/lib/x86_64-linux-gnu/pkgconfig"
# The Ubuntu libcap.pc records lib64 while its archive uses the multiarch directory.
export LIBRARY_PATH="$PWD/deps/root/usr/lib/x86_64-linux-gnu"
build-tools/bin/meson setup build source --buildtype=release \
  -Dselinux=disabled -Dman=disabled -Dtests=false \
  -Dbash_completion=disabled -Dzsh_completion=disabled \
  -Dc_link_args=-static -Dprefer_static=true
ninja -C build
python3 "$repo_root/tools/native_bwrap_archive.py" --binary build/bwrap \
  --upstream source --libcap-root deps/root --output native_bwrap.tar.gz
# Copy the validated archive to src/agentbridge/bundle/assets/ before packaging.
