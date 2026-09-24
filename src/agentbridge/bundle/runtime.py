"""Install reviewed wheel resources into a private, versioned runtime cache."""

from __future__ import annotations

import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import tarfile
import tempfile

from ..errors import BridgeError
from ..state_path import default_root


PACKAGE_ROOT = Path(__file__).resolve().parent
ARCHIVES = {
    "cli_proxy_api": "cli_proxy_api.tar.gz",
    "codex": "codex.tar.gz",
    "node": "node.tar.xz",
}
EXECUTABLES = {
    "cli_proxy_api": "cli-proxy-api",
    "codex": "bin/codex",
    "node": "bin/node",
}
MAX_FILES = 2000
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


def _platform():
    machine = platform.machine().lower()
    if platform.system() != "Linux" or machine not in {"x86_64", "aarch64", "arm64"}:
        raise BridgeError("unsupported_platform", "The bundled runtime supports Linux x86_64 and ARM64.")
    return "linux_aarch64" if machine in {"aarch64", "arm64"} else "linux_x86_64"


def _lock(package_root):
    try:
        value = json.loads((package_root / "lock.json").read_text())
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError("schema")
        return value
    except (OSError, UnicodeError, ValueError):
        raise BridgeError("bundled_runtime_invalid", "The bundled runtime lock is invalid.") from None


def _entry(component, package_root):
    lock = _lock(package_root)
    value = lock.get(component)
    if not isinstance(value, dict):
        raise BridgeError("bundled_runtime_unavailable", "The required bundled runtime is missing.")
    version = value.get("version") or value.get("commit")
    if not isinstance(version, str) or not SAFE_VERSION.fullmatch(version):
        raise BridgeError("bundled_runtime_invalid", "The bundled runtime version is invalid.")
    if component == "grantbridge":
        digest = value.get("source_sha256")
    else:
        assets = value.get("assets")
        asset = assets.get(_platform()) if isinstance(assets, dict) else None
        digest = asset.get("sha256") if isinstance(asset, dict) else None
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise BridgeError("bundled_runtime_invalid", "The bundled runtime digest is invalid.")
    return version, digest, value


def bundle_root(component, state_root=None, *, package_root=PACKAGE_ROOT):
    """Return the exact private cache directory selected by the reviewed lock."""
    version, digest, _ = _entry(component, Path(package_root))
    root = Path(default_root() if state_root is None else state_root).expanduser().resolve()
    return root / "bundled-runtimes" / f"{component}-{version}-{digest[:12]}"


def _private_parent(state_root):
    root = Path(state_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid():
        raise BridgeError("unsafe_store", "The AgentBridge runtime directory must belong to this user.")
    parent = root / "bundled-runtimes"
    if parent.is_symlink():
        raise BridgeError("unsafe_store", "The bundled runtime directory cannot be a symbolic link.")
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.stat().st_uid != os.getuid():
        raise BridgeError("unsafe_store", "The bundled runtime directory must belong to this user.")
    os.chmod(parent, 0o700)
    return parent


def _digest_file(path):
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(component, name, version, machine):
    if name.startswith("./"):
        name = name[2:]
    if component == "node":
        suffix = "arm64" if machine == "linux_aarch64" else "x64"
        prefix = f"node-{version}-linux-{suffix}/"
        if not name.startswith(prefix):
            return None
        name = name[len(prefix):]
        return name if name in {"bin/node", "LICENSE"} else None
    if component == "cli_proxy_api":
        return name if name in {"cli-proxy-api", "LICENSE"} else None
    if component == "codex":
        if name in {"codex-package.json", "LICENSE", "NOTICE"}:
            return name
        if name.startswith(("bin/", "codex-resources/", "codex-path/")):
            return name
    return None


def _safe_destination(stage, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise BridgeError("bundled_runtime_invalid", "The bundled runtime archive contains an unsafe path.")
    return stage.joinpath(*path.parts)


def _extract_archive(component, archive_path, stage, version, machine):
    files, unpacked = 0, 0
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive:
                relative = _relative(component, member.name, version, machine)
                if relative is None:
                    continue
                destination = _safe_destination(stage, relative)
                if member.isdir():
                    continue
                files += 1
                unpacked += member.size
                if (not member.isfile() or files > MAX_FILES or member.size < 0
                        or unpacked > MAX_UNPACKED_BYTES or destination.exists()):
                    raise BridgeError("bundled_runtime_invalid", "The bundled runtime archive is unsafe.")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise BridgeError("bundled_runtime_invalid", "The bundled runtime archive is incomplete.")
                with source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                executable = relative in {"cli-proxy-api", "bin/codex", "bin/node",
                                           "bin/codex-code-mode-host", "codex-path/rg",
                                           "codex-resources/bwrap", "codex-resources/zsh/bin/zsh"}
                destination.chmod(0o500 if executable else 0o400)
    except (OSError, EOFError, tarfile.TarError):
        raise BridgeError("bundled_runtime_invalid", "The bundled runtime archive is invalid.") from None
    binary = stage / EXECUTABLES[component]
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise BridgeError("bundled_runtime_invalid", "The bundled executable is missing.")
    return binary


def _grantbridge_sources(package_root, stage, entry):
    sources = entry.get("files")
    if not isinstance(sources, dict) or not sources:
        raise BridgeError("bundled_runtime_invalid", "The GrantBridge source lock is invalid.")
    combined = sha256()
    for relative, expected in sorted(sources.items()):
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise BridgeError("bundled_runtime_invalid", "The GrantBridge source lock is invalid.")
        destination = _safe_destination(stage, relative)
        source = package_root / "grantbridge" / relative
        if not source.is_file() or source.is_symlink() or _digest_file(source) != expected:
            raise BridgeError("bundled_runtime_invalid", "A bundled GrantBridge source differs from its lock.")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        destination.chmod(0o400)
        combined.update(relative.encode() + b"\0" + expected.encode() + b"\n")
    if combined.hexdigest() != entry.get("source_sha256"):
        raise BridgeError("bundled_runtime_invalid", "The GrantBridge source set differs from its lock.")
    adapter = stage / "scripts/agentbridge-proxy-adapter.mjs"
    if not adapter.is_file():
        raise BridgeError("bundled_runtime_invalid", "The GrantBridge adapter is missing.")
    return adapter


def _file_hashes(directory):
    files = {}
    for path in directory.rglob("*"):
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            continue
        if not stat.S_ISREG(details.st_mode):
            raise BridgeError("bundled_runtime_invalid", "The cached runtime contains an unsafe file.")
        relative = path.relative_to(directory).as_posix()
        if relative == ".verified.json":
            continue
        files[relative] = _digest_file(path)
    return files


def _install(component, state_root, package_root):
    package_root = Path(package_root)
    version, digest, entry = _entry(component, package_root)
    parent = _private_parent(state_root)
    target = bundle_root(component, state_root, package_root=package_root)
    primary = target / ("scripts/agentbridge-proxy-adapter.mjs" if component == "grantbridge"
                        else EXECUTABLES[component])
    lock_path = parent / ".install.lock"
    with lock_path.open("a+b") as lock_stream:
        fcntl.flock(lock_stream, fcntl.LOCK_EX)
        if target.exists():
            if target.is_symlink():
                raise BridgeError("bundled_runtime_invalid", "The cached bundled runtime is invalid.")
            marker = target / ".verified.json"
            try:
                value = json.loads(marker.read_text())
                if (marker.is_symlink() or
                        value != {"component": component, "digest": digest,
                                  "files": _file_hashes(target)}
                        or not primary.is_file()):
                    raise ValueError("invalid cache")
                return primary
            except (OSError, UnicodeError, ValueError, BridgeError):
                raise BridgeError("bundled_runtime_invalid", "The cached bundled runtime is invalid.") from None
        stage = Path(tempfile.mkdtemp(prefix=f".{component}-", dir=parent))
        try:
            if component == "grantbridge":
                installed = _grantbridge_sources(package_root, stage, entry)
            else:
                archive = package_root / "assets" / ARCHIVES[component]
                if not archive.is_file() or archive.is_symlink() or _digest_file(archive) != digest:
                    raise BridgeError("bundled_runtime_unavailable", "The reviewed bundled runtime archive is unavailable.")
                installed = _extract_archive(component, archive, stage, version, _platform())
            marker = stage / ".verified.json"
            marker.write_text(json.dumps({"component": component, "digest": digest,
                                          "files": _file_hashes(stage)}, sort_keys=True))
            marker.chmod(0o400)
            os.replace(stage, target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return primary


def resolve_binary(component, state_root=None, *, package_root=PACKAGE_ROOT):
    """Return an absolute executable from the pinned wheel, never from PATH."""
    if component not in EXECUTABLES:
        raise ValueError("Unknown bundled component")
    root = default_root() if state_root is None else state_root
    return _install(component, root, package_root)


def resolve_grantbridge_adapter(state_root=None, *, package_root=PACKAGE_ROOT):
    root = default_root() if state_root is None else state_root
    return _install("grantbridge", root, package_root)


def is_bundled_codex(path, state_root):
    """Identify the single reviewed Codex package allowed inside private state."""
    expected = bundle_root("codex", state_root)
    if Path(path).absolute() != expected / "bin/codex":
        return None
    return expected if resolve_binary("codex", state_root) == Path(path).absolute() else None
