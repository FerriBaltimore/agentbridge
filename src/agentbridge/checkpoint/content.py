"""Immutable private content catalog. References never resolve arbitrary caller paths."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile

from .native import MAX_BYTES, PATH, digest, fail, private_directory
from .state import canonical_uuid


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_tree(root):
    """Persist nested file entries bottom-up before publishing a restored tree."""
    for folder, _, files in os.walk(root, topdown=False):
        for name in files:
            descriptor = os.open(Path(folder) / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        sync_directory(folder)


def directory(store):
    return private_directory(store.root / 'checkpoints')


def object_path(store, content_id):
    canonical_uuid(content_id)
    return private_directory(directory(store) / 'objects') / (content_id + '.blob')


@contextmanager
def instance_lock(store, instance_id, *, busy_code='checkpoint_busy', shared=False):
    if not isinstance(instance_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',
                                                         instance_id):
        fail('checkpoint_invalid')
    path = private_directory(directory(store) / 'locks') / (instance_id + '.lock')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            fail(busy_code)
        yield
    finally:
        os.close(descriptor)


def publish(store, content_id, staged):
    target = object_path(store, content_id)
    checksum, size = digest(staged)
    with staged.open('rb') as source:
        os.fsync(source.fileno())
    staged.chmod(0o400)
    try:
        os.link(staged, target)
    except FileExistsError:
        if digest(target) != (checksum, size):
            fail('checkpoint_corrupt')
    sync_directory(target.parent)
    return {'content_id': content_id, 'sha256': checksum, 'bytes': size}


def resolve(store, content):
    if not isinstance(content, dict) or set(content) != {'content_id', 'sha256', 'bytes'}:
        fail('checkpoint_invalid')
    path = object_path(store, content['content_id'])
    if digest(path) != (content['sha256'], content['bytes']):
        fail('checkpoint_corrupt')
    return path


def stage_archive(store, content_id, native_root, files):
    descriptor, name = tempfile.mkstemp(prefix='.archive-', dir=directory(store))
    staged = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            with tarfile.open(fileobj=target, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for record in files:
                    info = tarfile.TarInfo(record['path'])
                    info.size, info.mode = record['bytes'], 0o600
                    with (native_root / record['path']).open('rb') as source:
                        archive.addfile(info, source)
            target.flush()
            os.fsync(target.fileno())
        return publish(store, content_id, staged)
    finally:
        staged.unlink(missing_ok=True)


def extract(store, descriptor, destination):
    path = resolve(store, descriptor['content'])
    expected = {item['path']: item for item in descriptor['files']}
    if len(expected) != len(descriptor['files']):
        fail('checkpoint_invalid')
    seen, total = set(), 0
    with tarfile.open(path, 'r:') as archive:
        for member in archive:
            relative = member.name
            if (relative not in expected or relative in seen or not PATH.fullmatch(relative)
                    or any(part in {'.', '..'} for part in relative.split('/'))
                    or not member.isfile() or member.issparse()):
                fail('checkpoint_unsafe_path')
            record = expected[relative]
            total += member.size
            if member.size != record['bytes'] or total > MAX_BYTES:
                fail('checkpoint_corrupt')
            target = destination / relative
            private_directory(target.parent)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as out, archive.extractfile(member) as source:
                remaining = member.size
                while remaining:
                    block = source.read(min(remaining, 1024 * 1024))
                    if not block:
                        fail('checkpoint_corrupt')
                    out.write(block)
                    remaining -= len(block)
                out.flush()
                os.fsync(out.fileno())
            if digest(target) != (record['sha256'], record['bytes']):
                fail('checkpoint_corrupt')
            seen.add(relative)
    if seen != set(expected):
        fail('checkpoint_incomplete')


def register(store, content, source):
    """Trusted host materialization after download/decryption; intentionally absent from RPC."""
    if digest(Path(source)) != (content['sha256'], content['bytes']):
        fail('checkpoint_corrupt')
    fd, name = tempfile.mkstemp(prefix='.import-', dir=directory(store))
    staged = Path(name)
    try:
        with os.fdopen(fd, 'wb') as out, Path(source).open('rb') as inp:
            for block in iter(lambda: inp.read(1024 * 1024), b''):
                out.write(block)
        actual = publish(store, content['content_id'], staged)
        if actual != content:
            fail('checkpoint_corrupt')
    finally:
        staged.unlink(missing_ok=True)


def publish_descriptor(store, descriptor):
    folder = private_directory(directory(store) / 'descriptors')
    path = folder / (canonical_uuid(descriptor['checkpoint_id']) + '.json')
    payload = json.dumps(descriptor, sort_keys=True, separators=(',', ':')).encode()
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            fail('checkpoint_corrupt')
        return
    fd, name = tempfile.mkstemp(prefix='.descriptor-', dir=folder)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        temporary.chmod(0o400)
        os.link(temporary, path)
        sync_directory(folder)
    finally:
        temporary.unlink(missing_ok=True)
