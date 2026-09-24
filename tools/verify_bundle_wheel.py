"""Verify that a built platform wheel contains every reviewed runtime asset."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
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


def verify_wheel(path):
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
        wheel_file = next((name for name in names if name.endswith(".dist-info/WHEEL")), None)
        if wheel_file is None or f"Tag: py3-none-manylinux_2_28_{matched.group(1)}" not in archive.read(wheel_file).decode():
            raise ValueError("The wheel metadata does not match its platform tag.")
    return {"wheel": path.name, "platform": arch,
            "components": {key: lock[key].get("version") or lock[key].get("commit")
                           for key in (*ASSETS, "grantbridge")}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", type=Path, nargs="+")
    arguments = parser.parse_args(argv)
    try:
        for wheel in arguments.wheels:
            print(json.dumps(verify_wheel(wheel), sort_keys=True))
    except (OSError, KeyError, ValueError) as error:
        parser.exit(1, f"Bundle wheel rejected: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
