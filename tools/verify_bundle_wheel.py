"""Verify that a built platform wheel contains every reviewed runtime asset."""

import argparse
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import tarfile
from zipfile import ZipFile


ASSETS = {
    "cli_proxy_api": "cli_proxy_api.tar.gz",
    "codex": "codex.tar.gz",
    "node": "node.tar.xz",
}
LICENSES = (
    "licenses/cli_proxy_api-license.txt",
    "licenses/codex-license.txt",
    "licenses/codex-notice.txt",
    "licenses/node-license.txt",
    "grantbridge/LICENSE",
)
PLATFORMS = re.compile(r"-py3-none-manylinux_2_28_(x86_64|aarch64)\.whl\Z")


def _verify_native_launcher(archive, names, base, lock, arch):
    asset = lock["native_bwrap"]["assets"].get(arch)
    member = base + "assets/native_bwrap.tar.gz"
    if asset is None:
        if member in names:
            raise ValueError("The wheel includes a native launcher for an unreviewed platform.")
        return False
    if member not in names:
        raise ValueError("The wheel omits its reviewed native launcher.")
    data = archive.read(member)
    if sha256(data).hexdigest() != asset["sha256"]:
        raise ValueError("The native launcher archive differs from its lock.")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as source:
        members = source.getmembers()
        if ({item.name for item in members} != {"bin/bwrap", "LICENSE"}
                or len(members) != 2 or any(not item.isfile() for item in members)):
            raise ValueError("The native launcher archive has unexpected members.")
        binary = source.extractfile("bin/bwrap").read()
        license_data = source.extractfile("LICENSE").read()
    if sha256(binary).hexdigest() != asset["binary_sha256"]:
        raise ValueError("The native launcher binary differs from its lock.")
    license_member = base + "licenses/native_bwrap-license.txt"
    if license_member not in names or archive.read(license_member) != license_data:
        raise ValueError("The native launcher license is missing or differs from its archive.")
    return True


def verify_wheel(path, *, grantbridge_archive=None, grantbridge_manifest=None):
    path = Path(path)
    matched = PLATFORMS.search(path.name)
    if matched is None:
        raise ValueError("The AgentBridge wheel has an unsupported platform tag.")
    arch = "linux_" + matched.group(1)
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        bases = {name.removesuffix("lock.json") for name in names
                 if name.endswith("/agentbridge/bundle/lock.json")}
        if len(bases) != 1:
            raise ValueError("The wheel has no unique bundled runtime lock.")
        base = bases.pop()
        lock = json.loads(archive.read(base + "lock.json"))
        if lock.get("schema") != 1:
            raise ValueError("The bundled runtime lock schema is invalid.")
        for component, filename in ASSETS.items():
            member = base + "assets/" + filename
            expected = lock[component]["assets"][arch]["sha256"]
            if member not in names or sha256(archive.read(member)).hexdigest() != expected:
                raise ValueError(f"The {component} archive is absent or differs from its lock.")
        grantbridge = lock["grantbridge"]
        if (not re.fullmatch(r'[0-9a-f]{64}', str(grantbridge.get('artifact_sha256')))
                or not re.fullmatch(r'[0-9a-f]{40}', str(grantbridge.get('commit')))
                or not isinstance(grantbridge.get('version'), str)):
            raise ValueError('The GrantBridge artifact provenance is incomplete.')
        if grantbridge_archive is not None:
            if __package__:
                from .import_grantbridge import derive
            else:
                from import_grantbridge import derive

            derived, _ = derive(grantbridge_archive, grantbridge_manifest,
                                expected_sha256=grantbridge['artifact_sha256'])
            if derived != grantbridge:
                raise ValueError('The bundled GrantBridge closure differs from its upstream artifact.')
        combined = sha256()
        for relative, expected in sorted(grantbridge["files"].items()):
            member = base + "grantbridge/" + relative
            if member not in names or sha256(archive.read(member)).hexdigest() != expected:
                raise ValueError("A GrantBridge source differs from its lock.")
            combined.update(relative.encode() + b"\0" + expected.encode() + b"\n")
        if combined.hexdigest() != grantbridge["source_sha256"]:
            raise ValueError("The GrantBridge source set differs from its lock.")
        if any(base + relative not in names for relative in LICENSES):
            raise ValueError("The wheel omits a required upstream license or notice.")
        native = _verify_native_launcher(archive, names, base, lock, arch)
        wheel_file = next((name for name in names if name.endswith(".dist-info/WHEEL")), None)
        if wheel_file is None or f"Tag: py3-none-manylinux_2_28_{matched.group(1)}" not in archive.read(wheel_file).decode():
            raise ValueError("The wheel metadata does not match its platform tag.")
    components = [*ASSETS, "grantbridge"]
    if native:
        components.append("native_bwrap")
    return {"wheel": path.name, "platform": arch,
            "components": {key: lock[key].get("version") or lock[key].get("commit")
                           for key in components}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", type=Path, nargs="+")
    parser.add_argument('--grantbridge-archive', type=Path)
    parser.add_argument('--grantbridge-manifest', type=Path)
    arguments = parser.parse_args(argv)
    try:
        for wheel in arguments.wheels:
            print(json.dumps(verify_wheel(wheel,
                grantbridge_archive=arguments.grantbridge_archive,
                grantbridge_manifest=arguments.grantbridge_manifest), sort_keys=True))
    except (OSError, KeyError, ValueError) as error:
        parser.exit(1, f"Bundle wheel rejected: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
