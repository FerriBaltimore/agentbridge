"""Extract the checksum-pinned GrantBridge runtime, including its browser driver and assets."""

from hashlib import sha256
from pathlib import PurePosixPath
import re
import shutil
import tarfile

from ..errors import BridgeError


def extract_runtime(package_root, stage, entry):
    archive_path = package_root / 'assets' / 'grantbridge.tar.gz'
    expected = entry.get('runtime_sha256', '')
    if not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise BridgeError('bundled_runtime_invalid', 'The browser runtime digest is invalid.')
    digest = sha256()
    try:
        if archive_path.is_symlink():
            raise ValueError()
        with archive_path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError()
        files, size = 0, 0
        with tarfile.open(archive_path, 'r:gz') as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                files += 1
                size += member.size
                if (not member.isfile() or path.is_absolute() or '..' in path.parts
                        or str(path) != member.name or '\\' in member.name
                        or files > 12000 or not 0 <= size <= 512 * 1024 * 1024):
                    raise ValueError()
                target = stage.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o400)
        adapter = stage / 'scripts/agentbridge-proxy-adapter.mjs'
        for required in (adapter, stage / 'node_modules/playwright-core/package.json',
                         stage / 'src/browser/viewer/browser.html'):
            if not required.is_file():
                raise ValueError()
        return adapter
    except (OSError, ValueError, EOFError, tarfile.TarError):
        raise BridgeError('bundled_runtime_invalid',
                          'The pinned browser runtime is missing or invalid.') from None
