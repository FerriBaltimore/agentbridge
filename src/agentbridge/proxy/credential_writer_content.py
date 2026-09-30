"""Validate bounded private exports from the credential writer before materialization."""

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import PurePosixPath
import re
import stat
import tarfile

from ..checkpoint import native
from .credential_content import fail

MAX_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE = MAX_BYTES + 16 * 1024 * 1024
MAX_FILES = 10000
MANIFEST = 'credential-files.json'


def revision(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        fail()
    return value


def relative(value):
    if (not isinstance(value, str) or not value or value.startswith('/') or '\\' in value
            or any(part in {'', '.', '..'} for part in value.split('/'))
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part.startswith('.oauth') for part in PurePosixPath(value).parts)):
        fail()
    return value


@contextmanager
def opened_export(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > MAX_ARCHIVE):
            fail()
        with tarfile.open(fileobj=source, mode='r:') as archive:
            yield archive


def read_export(path, *, destination=None, account_id=None):
    """Hash every regular entry; extraction is optional and never uses tarfile.extract."""
    with opened_export(path) as archive:
        first = archive.next()
        if (not first or first.name != MANIFEST or not first.isfile() or first.issparse()
                or first.size > 4 * 1024 * 1024):
            fail()
        manifest = json.load(archive.extractfile(first))
        if (not isinstance(manifest, dict)
                or set(manifest) != {'format_version', 'revision', 'files'}
                or manifest['format_version'] != '1' or not isinstance(manifest['files'], list)
                or len(manifest['files']) > MAX_FILES):
            fail()
        revision(manifest['revision'])
        expected, total = {}, 0
        for item in manifest['files']:
            if (not isinstance(item, dict) or set(item) != {'path', 'sha256', 'bytes'}
                    or type(item['bytes']) is not int or item['bytes'] < 0):
                fail()
            name = relative(item['path'])
            revision(item['sha256'])
            total += item['bytes']
            if name in expected or total > MAX_BYTES:
                fail()
            expected[name] = item
        seen, files = set(), []
        while (member := archive.next()) is not None:
            name = relative(member.name)
            if (name not in expected or name in seen or not member.isfile() or member.issparse()
                    or member.size != expected[name]['bytes']):
                fail()
            seen.add(name)
            checksum, target = hashlib.sha256(), None
            relative_path = f'accounts/{account_id}/auth/{name}'
            if destination is not None:
                path = destination / relative_path
                native.private_directory(path.parent)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                target = os.fdopen(fd, 'wb')
            try:
                with archive.extractfile(member) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        checksum.update(block)
                        if target is not None:
                            target.write(block)
                if checksum.hexdigest() != expected[name]['sha256']:
                    fail()
                if target is not None:
                    target.flush()
                    os.fsync(target.fileno())
            finally:
                if target is not None:
                    target.close()
            files.append({**expected[name], 'path': relative_path})
        if seen != set(expected):
            fail()
        return manifest['revision'], sorted(files, key=lambda item: item['path'])
