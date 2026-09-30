"""Private immutable capsules of CLIProxyAPI auth files, excluding runtime authority."""

import io
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile

from ..checkpoint import content, native
from ..errors import BridgeError
from ..models import identifier

MAX_BYTES = 512 * 1024 * 1024
MAX_FILES = 10000
MANIFEST = 'credential-capture.json'


def fail():
    raise BridgeError('credential_snapshot_corrupt', 'The private credential capture is invalid.')


def walk_error(_error):
    fail()


def inventory(root, account_ids):
    files = []
    total = 0
    for account_id in account_ids:
        identifier(account_id)
        home = root / 'accounts' / account_id / 'auth'
        if not home.is_dir() or any(path.is_symlink() for path in (home, *home.parents)):
            fail()
        for folder, directories, names in os.walk(home, followlinks=False, onerror=walk_error):
            if any((Path(folder) / name).is_symlink() for name in directories):
                fail()
            for name in sorted(names):
                path = Path(folder) / name
                if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                    fail()
                checksum, size = native.digest(path)
                total += size
                if total > MAX_BYTES or len(files) >= MAX_FILES:
                    fail()
                files.append({'path': path.relative_to(root).as_posix(),
                              'sha256': checksum, 'bytes': size})
    return sorted(files, key=lambda item: item['path'])


def capture(store, account_ids, snapshot):
    root = store.root / 'managed-proxies'
    before = inventory(root, account_ids)
    stage = Path(tempfile.mkdtemp(prefix='.proxy-auth-', dir=content.directory(store)))
    try:
        for item in before:
            source, target = root / item['path'], stage / item['path']
            native.private_directory(target.parent)
            shutil.copyfile(source, target)
            target.chmod(0o600)
            if native.digest(target) != (item['sha256'], item['bytes']):
                fail()
        if inventory(root, account_ids) != before:
            fail()
        archive = stage / 'credentials.tar'
        manifest = json.dumps({'snapshot': snapshot, 'files': before}, sort_keys=True).encode()
        with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT) as target:
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mode = len(manifest), 0o600
            target.addfile(info, io.BytesIO(manifest))
            for item in before:
                info = tarfile.TarInfo(item['path'])
                info.size, info.mode = item['bytes'], 0o600
                with (stage / item['path']).open('rb') as source:
                    target.addfile(info, source)
        return content.publish(store, snapshot['snapshot_id'], archive)
    finally:
        shutil.rmtree(stage)


def read(store, reference, destination=None):
    archive_path = content.resolve(store, reference)
    with tarfile.open(archive_path, 'r:') as archive:
        first = archive.next()
        if (not first or first.name != MANIFEST or not first.isfile() or first.issparse()
                or first.size > 4 * 1024 * 1024):
            fail()
        manifest = json.load(archive.extractfile(first))
        snapshot, files = manifest['snapshot'], manifest['files']
        expected = {item['path']: item for item in files}
        if len(expected) != len(files) or len(files) > MAX_FILES:
            fail()
        seen = set()
        total = 0
        while (item := archive.next()) is not None:
            parts = PurePosixPath(item.name).parts
            if (item.name not in expected or item.name in seen or not item.isfile() or item.issparse()
                    or len(parts) < 4 or parts[0] != 'accounts' or parts[2] != 'auth'
                    or parts[1] not in snapshot['credential_refs']
                    or item.name.startswith('/') or '\\' in item.name
                    or any(part in {'', '.', '..'} for part in item.name.split('/'))
                    or any(ord(char) < 32 or ord(char) == 127 for char in item.name)):
                fail()
            record = expected[item.name]
            total += item.size
            if item.size != record['bytes'] or total > MAX_BYTES:
                fail()
            seen.add(item.name)
            digest = hashlib.sha256()
            out = None
            if destination is not None:
                target = destination / item.name
                native.private_directory(target.parent)
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                out = os.fdopen(fd, 'wb')
            try:
                with archive.extractfile(item) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(block)
                        if out is not None:
                            out.write(block)
                if out is not None:
                    out.flush()
                    os.fsync(out.fileno())
                if digest.hexdigest() != record['sha256']:
                    fail()
            finally:
                if out is not None:
                    out.close()
        if seen != set(expected):
            fail()
        return snapshot
