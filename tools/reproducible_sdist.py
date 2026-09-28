"""Rewrite an sdist tarball so equal inputs give equal bytes when SOURCE_DATE_EPOCH is set.

setuptools writes the sdist with the checkout's file times, the building user's uid, gid and
names, and the current time in the gzip header. None of that is part of the release, so the
build backend passes its sdist through `normalize_tarball` when `SOURCE_DATE_EPOCH` is set.
Wheels already take their zip timestamps from that variable.
"""

import gzip
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

CHUNK = 1024 * 1024


def source_date_epoch():
    """The reproducible timestamp requested by the environment, or None."""
    value = os.environ.get('SOURCE_DATE_EPOCH', '').strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError('SOURCE_DATE_EPOCH must be a non-negative integer of seconds.')
    return int(value)


def _normalized(member, epoch):
    """A fresh header: no inherited pax records, owner 0, mode 0644/0755, fixed mtime."""
    fresh = tarfile.TarInfo(member.name)
    fresh.type = member.type
    fresh.size = member.size if member.isfile() else 0
    fresh.linkname = member.linkname
    fresh.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
    fresh.mtime = epoch
    fresh.uid = fresh.gid = 0
    fresh.uname = fresh.gname = ''
    return fresh


def normalize_tarball(path, epoch):
    """Sorted members, owner 0, mtime `epoch`, gzip header without a time; same contents."""
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix='.sdist-normalize-', dir=path.parent) as work:
        plain = Path(work) / 'source.tar'
        with gzip.open(path, 'rb') as compressed, plain.open('wb') as output:
            shutil.copyfileobj(compressed, output, CHUNK)
        rewritten = Path(work) / 'normalized.tar'
        with tarfile.open(plain, 'r:') as source, \
                tarfile.open(rewritten, 'w', format=tarfile.PAX_FORMAT) as target:
            for member in sorted(source.getmembers(), key=lambda item: item.name):
                if not (member.isfile() or member.isdir()):
                    raise ValueError(f'The sdist has a non-regular member: {member.name}')
                target.addfile(_normalized(member, epoch),
                               source.extractfile(member) if member.isfile() else None)
        with rewritten.open('rb') as data, path.open('wb') as output, gzip.GzipFile(
                filename='', mode='wb', fileobj=output, mtime=0, compresslevel=9) as stream:
            shutil.copyfileobj(data, stream, CHUNK)
    return path
