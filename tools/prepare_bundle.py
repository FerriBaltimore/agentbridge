"""Build one Linux wheel from exact, checksum-pinned upstream runtime archives."""

from __future__ import annotations

import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/agentbridge/bundle"
CACHE = ROOT / "dist/bundle-cache"
ARCHIVES = {
    "cli_proxy_api": "cli_proxy_api.tar.gz",
    "codex": "codex.tar.gz",
    "node": "node.tar.xz",
}
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


def target_arch():
    value = os.environ.get("AGENTBRIDGE_TARGET_ARCH", platform.machine().lower())
    if value == "arm64":
        value = "aarch64"
    if value not in {"x86_64", "aarch64"}:
        raise ValueError("Choose Linux x86_64 or aarch64 as AGENTBRIDGE_TARGET_ARCH.")
    if platform.system() != "Linux":
        raise ValueError("Bundled AgentBridge wheels currently target Linux only.")
    return f"linux_{value}"


def _digest(path):
    result = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _download(url, destination, expected):
    if destination.is_file() and _digest(destination) == expected:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    total = 0
    digest = sha256()
    try:
        with urlopen(url, timeout=60) as response, temporary.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("An upstream runtime archive exceeded the size limit.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected:
            raise ValueError("An upstream runtime archive differs from its pinned SHA-256.")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _asset(entry, arch):
    asset = entry.get("assets", {}).get(arch)
    if not isinstance(asset, dict) or not isinstance(asset.get("url"), str):
        raise ValueError(f"The runtime lock has no {arch} asset.")
    digest = asset.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("The runtime lock has an invalid asset digest.")
    return asset["url"], digest


def _validate_archive(component, path, entry, arch):
    sys.path.insert(0, str(ROOT / "src"))
    from agentbridge.bundle.runtime import _extract_archive

    with tempfile.TemporaryDirectory(prefix="agentbridge-inspect-") as temporary:
        binary = _extract_archive(component, path, Path(temporary), entry["version"], arch)
        with binary.open("rb") as source:
            header = source.read(20)
        machine = 183 if arch == "linux_aarch64" else 62
        if (len(header) != 20 or header[:6] != b"\x7fELF\x02\x01"
                or int.from_bytes(header[18:20], "little") != machine):
            raise ValueError(f"The {component} binary does not match {arch}.")


def _copy_license_from_archive(component, path, entry, arch, destination):
    import tarfile

    with tarfile.open(path, "r:*") as archive:
        expected = "LICENSE" if component == "cli_proxy_api" else (
            f"node-{entry['version']}-linux-{'arm64' if arch == 'linux_aarch64' else 'x64'}/LICENSE")
        member = archive.getmember(expected)
        if not member.isfile() or member.size > 4 * 1024 * 1024:
            raise ValueError(f"The {component} license is invalid.")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError(f"The {component} license is missing.")
        destination.write_bytes(source.read())


def prepare_bundle():
    arch = target_arch()
    lock = json.loads((PACKAGE / "lock.json").read_text())
    if lock.get("schema") != 1:
        raise ValueError("The runtime lock schema is invalid.")
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / ".build.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        assets = PACKAGE / "assets"
        licenses = PACKAGE / "licenses"
        assets.mkdir(exist_ok=True)
        licenses.mkdir(exist_ok=True)
        for component, filename in ARCHIVES.items():
            entry = lock[component]
            url, digest = _asset(entry, arch)
            target = assets / filename
            cached = target if target.is_file() and _digest(target) == digest else CACHE / arch / filename
            if cached != target:
                _download(url, cached, digest)
            _validate_archive(component, cached, entry, arch)
            if cached != target:
                shutil.copyfile(cached, target)
            if component in {"cli_proxy_api", "node"}:
                _copy_license_from_archive(component, cached, entry, arch,
                                           licenses / f"{component}-license.txt")
        codex = lock["codex"]
        for name, reference in codex.get("licenses", {}).items():
            _download(reference["url"], licenses / f"codex-{name.lower()}.txt",
                      reference["sha256"])
        sys.path.insert(0, str(ROOT / "src"))
        from agentbridge.bundle.runtime import _grantbridge_sources

        with tempfile.TemporaryDirectory(prefix="agentbridge-grantbridge-") as temporary:
            _grantbridge_sources(PACKAGE, Path(temporary), lock["grantbridge"])
    return arch


if __name__ == "__main__":
    print(f"Prepared {prepare_bundle()} AgentBridge wheel resources.")
