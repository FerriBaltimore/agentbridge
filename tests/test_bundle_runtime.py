"""Offline integrity checks for the wheel's private runtime installer."""

from hashlib import sha256
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

from agentbridge.bundle import runtime
from agentbridge.errors import BridgeError


def _digest(path):
    return sha256(path.read_bytes()).hexdigest()


def _write_archive(path, members, *, symlink=None):
    mode = "w:xz" if path.suffix == ".xz" else "w:gz"
    with tarfile.open(path, mode) as archive:
        for name, content in members.items():
            data = content.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name.endswith(("/node", "/codex")) else 0o644
            archive.addfile(member, io.BytesIO(data))
        if symlink is not None:
            member = tarfile.TarInfo(symlink)
            member.type = tarfile.SYMTYPE
            member.linkname = "/tmp/untrusted"
            archive.addfile(member)


@pytest.fixture
def bundle_package(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "x86_64")
    package = tmp_path / "package"
    assets = package / "assets"
    assets.mkdir(parents=True)
    archives = {
        "cli_proxy_api": ("cli_proxy_api.tar.gz", "7.3.16", {
            "cli-proxy-api": "fixture proxy executable", "LICENSE": "fixture proxy license",
        }),
        "codex": ("codex.tar.gz", "0.153.0", {
            "bin/codex": "fixture codex executable",
            "codex-package.json": "{}",
            "LICENSE": "fixture codex license",
            "NOTICE": "fixture codex notice",
        }),
        "node": ("node.tar.xz", "v24.21.0", {
            "node-v24.21.0-linux-x64/bin/node": "fixture node executable",
            "node-v24.21.0-linux-x64/LICENSE": "fixture node license",
        }),
    }
    lock = {"schema": 1}
    for component, (filename, version, members) in archives.items():
        archive = assets / filename
        _write_archive(archive, members)
        digest = _digest(archive)
        lock[component] = {
            "version": version,
            "assets": {
                "linux_x86_64": {"sha256": digest},
                "linux_aarch64": {"sha256": digest},
            },
        }
    source_files = {
        "LICENSE": "fixture GrantBridge license",
        "scripts/agentbridge-proxy-adapter.mjs": "fixture adapter",
        "src/agentbridge-proxy.mjs": "fixture implementation",
    }
    source_hashes = {}
    for relative, content in source_files.items():
        source = package / "grantbridge" / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content)
        source_hashes[relative] = _digest(source)
    combined = sha256()
    for relative, digest in sorted(source_hashes.items()):
        combined.update(relative.encode() + b"\0" + digest.encode() + b"\n")
    lock["grantbridge"] = {
        "commit": "fixturecommit",
        "files": source_hashes,
        "source_sha256": combined.hexdigest(),
    }
    (package / "lock.json").write_text(json.dumps(lock))
    return package


def _error_code(operation):
    with pytest.raises(BridgeError) as caught:
        operation()
    return caught.value.code


def test_install_uses_locked_archives_and_private_cache(bundle_package, tmp_path):
    state = tmp_path / "state"
    expected = {
        "cli_proxy_api": ("cli-proxy-api", b"fixture proxy executable"),
        "codex": ("bin/codex", b"fixture codex executable"),
        "node": ("bin/node", b"fixture node executable"),
    }
    for component, (relative, content) in expected.items():
        binary = runtime.resolve_binary(component, state, package_root=bundle_package)
        assert binary == runtime.bundle_root(component, state, package_root=bundle_package) / relative
        assert binary.read_bytes() == content
        assert os.access(binary, os.X_OK)
        assert binary.stat().st_mode & 0o777 == 0o500
        assert runtime.resolve_binary(component, state, package_root=bundle_package) == binary
    adapter = runtime.resolve_grantbridge_adapter(state, package_root=bundle_package)
    assert adapter.read_text() == "fixture adapter"
    assert adapter.stat().st_mode & 0o777 == 0o400
    assert (state / "bundled-runtimes").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("machine,expected", [
    ("x86_64", "linux_x86_64"),
    ("aarch64", "linux_aarch64"),
    ("arm64", "linux_aarch64"),
])
def test_selects_linux_architecture(monkeypatch, machine, expected):
    monkeypatch.setattr(runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runtime.platform, "machine", lambda: machine)
    assert runtime._platform() == expected


@pytest.mark.parametrize("system,machine", [("Darwin", "x86_64"), ("Linux", "armv7l")])
def test_rejects_unsupported_platform(monkeypatch, system, machine):
    monkeypatch.setattr(runtime.platform, "system", lambda: system)
    monkeypatch.setattr(runtime.platform, "machine", lambda: machine)
    assert _error_code(runtime._platform) == "unsupported_platform"


def test_architecture_selects_its_own_locked_digest(bundle_package, monkeypatch, tmp_path):
    lock_path = bundle_package / "lock.json"
    lock = json.loads(lock_path.read_text())
    different_digest = "a" * 64
    lock["cli_proxy_api"]["assets"]["linux_aarch64"]["sha256"] = different_digest
    lock_path.write_text(json.dumps(lock))
    monkeypatch.setattr(runtime.platform, "machine", lambda: "aarch64")
    assert runtime.bundle_root("cli_proxy_api", tmp_path, package_root=bundle_package).name.endswith(
        different_digest[:12])
    assert _error_code(lambda: runtime.resolve_binary(
        "cli_proxy_api", tmp_path / "state", package_root=bundle_package)) == (
            "bundled_runtime_unavailable")


@pytest.mark.parametrize("change", ["modify", "extra_file", "symlink"])
def test_rejects_cache_tampering(bundle_package, tmp_path, change):
    state = tmp_path / "state"
    binary = runtime.resolve_binary("cli_proxy_api", state, package_root=bundle_package)
    if change == "modify":
        binary.chmod(0o700)
        binary.write_text("changed executable")
    elif change == "extra_file":
        (binary.parent / "unexpected.txt").write_text("unexpected")
    else:
        (binary.parent / "unexpected").symlink_to("/tmp/untrusted")
    assert _error_code(lambda: runtime.resolve_binary(
        "cli_proxy_api", state, package_root=bundle_package)) == "bundled_runtime_invalid"


def test_rejects_symlink_in_selected_archive(bundle_package, tmp_path):
    archive = bundle_package / "assets/cli_proxy_api.tar.gz"
    _write_archive(archive, {"LICENSE": "fixture license"}, symlink="cli-proxy-api")
    lock_path = bundle_package / "lock.json"
    lock = json.loads(lock_path.read_text())
    lock["cli_proxy_api"]["assets"]["linux_x86_64"]["sha256"] = _digest(archive)
    lock_path.write_text(json.dumps(lock))
    assert _error_code(lambda: runtime.resolve_binary(
        "cli_proxy_api", tmp_path / "state", package_root=bundle_package)) == (
            "bundled_runtime_invalid")


def test_rejects_grantbridge_source_not_matching_lock(bundle_package, tmp_path):
    source = bundle_package / "grantbridge/src/agentbridge-proxy.mjs"
    source.write_text("changed source")
    assert _error_code(lambda: runtime.resolve_grantbridge_adapter(
        tmp_path / "state", package_root=bundle_package)) == "bundled_runtime_invalid"


def test_missing_archive_never_uses_path_executable(bundle_package, tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "cli-proxy-api").write_text("fake PATH executable")
    monkeypatch.setenv("PATH", str(fake))
    (bundle_package / "assets/cli_proxy_api.tar.gz").unlink()
    assert _error_code(lambda: runtime.resolve_binary(
        "cli_proxy_api", tmp_path / "state", package_root=bundle_package)) == (
            "bundled_runtime_unavailable")


def test_rejects_unknown_component_without_path_lookup(bundle_package, tmp_path):
    with pytest.raises(ValueError, match="Unknown bundled component"):
        runtime.resolve_binary("unknown", tmp_path, package_root=bundle_package)
