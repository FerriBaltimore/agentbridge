"""Pinned Codex native-home inventory and coherent private file materialization."""

from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time

from ..codex_executable import codex_argv
from ..errors import BridgeError
from ..models import Account
from ..native import compatible, locate
from .state import canonical_uuid

NATIVE_SCHEMA = 'codex-0-153-0-home-v1'
MAX_BYTES = 8 * 1024 * 1024 * 1024
MAX_FILES = 100_000
PATH = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z')
EPHEMERAL = {'tmp', 'logs', 'shell_snapshots', '.tmp'}
LOCAL_CONFIGURATION = {'auth.json', 'config.toml', '.credentials.json',
                       '.agentbridge-checkpoint.json'}


def fail(code='checkpoint_incompatible'):
    mapped = {'checkpoint_unsafe_path': 'checkpoint_corrupt',
              'checkpoint_incompatible': 'runtime_incompatible',
              'checkpoint_scope_mismatch': 'scope_mismatch',
              'checkpoint_invalid': 'checkpoint_incomplete',
              'checkpoint_too_large': 'checkpoint_incomplete'}.get(code, code)
    raise BridgeError(mapped, 'The native checkpoint cannot be verified.', phase='checkpoint',
                      retryable=mapped in {'checkpoint_busy', 'checkpoint_incomplete'})


def digest(path):
    value, size = sha256(), 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            fail('checkpoint_unsafe_path')
        for block in iter(lambda: source.read(1024 * 1024), b''):
            size += len(block)
            if size > MAX_BYTES:
                fail('checkpoint_too_large')
            value.update(block)
    return value.hexdigest(), size


def private_directory(path):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        fail('checkpoint_unsafe_path')
    missing = []
    for parent in (path, *path.parents):
        if parent.exists():
            break
        missing.append(parent)
    for parent in reversed(missing):
        parent.mkdir(mode=0o700, exist_ok=True)
        from .content import sync_directory

        sync_directory(parent)
        sync_directory(parent.parent)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        fail('checkpoint_unsafe_path')
    path.chmod(0o700)
    return path


def inventory(home):
    """Capture all regular native-home state, excluding only regenerable runtime debris.

    SQLite journals are represented by a transactional backup of their owning database.
    Unknown special entries are refused, rather than followed or silently called durable.
    """
    home = Path(home)
    if not home.is_dir() or any(p.is_symlink() for p in (home, *home.parents)):
        fail('checkpoint_unsafe_path')
    result = []
    for folder, directories, files in os.walk(
            home, followlinks=False, onerror=lambda error: fail('checkpoint_incomplete')):
        directory = Path(folder)
        kept = []
        for name in directories:
            child = directory / name
            if child.is_symlink():
                fail('checkpoint_unsafe_path')
            if name not in EPHEMERAL:
                kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            path = directory / name
            relative = path.relative_to(home).as_posix()
            if not PATH.fullmatch(relative) or any(p in {'.', '..'} for p in path.parts):
                fail('checkpoint_unsafe_path')
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                fail('checkpoint_unsafe_path')
            if name.endswith(('-wal', '-shm', '-journal')):
                base = path.with_name(name.rsplit('-', 1)[0])
                if not base.is_file() or base.is_symlink():
                    fail('checkpoint_incomplete')
                continue
            if (name.endswith(('.lock', '.pid', '.sock')) or name == 'version.json'
                    or relative in LOCAL_CONFIGURATION):
                continue
            result.append((relative, path))
            if len(result) > MAX_FILES:
                fail('checkpoint_too_large')
    return sorted(result)


def fingerprint(home):
    records = []
    total = 0
    for relative, path in inventory(home):
        checksum, size = digest(path)
        total += size
        if total > MAX_BYTES:
            fail('checkpoint_too_large')
        records.append((relative, checksum, size))
        # A journal can change without touching its main database; include it in stability.
        for suffix in ('-wal', '-journal'):
            journal = path.with_name(path.name + suffix)
            if journal.exists():
                records.append((relative + suffix, *digest(journal)))
    return sha256(json.dumps(records, separators=(',', ':')).encode()).hexdigest()


def runtime(store, session, *, home=None):
    native_id = canonical_uuid(session['native_id'])
    home = home or store.root / 'codex-runtime' / session['id']
    rollout = locate('codex', home, native_id)
    account = Account(**json.loads(store.get('accounts', session['account_id'])['config']))
    command = codex_argv(account, store.root)
    if not compatible('codex', rollout, command, native_id):
        fail()
    with rollout.open('rb') as source:
        first = source.readline(1024 * 1024)
    try:
        metadata = json.loads(first)
        if (metadata['type'] != 'session_meta'
                or metadata['payload']['cli_version'] != '0.153.0'):
            fail()
    except (ValueError, KeyError, TypeError):
        fail()
    launcher = sha256()
    for index, argument in enumerate(command):
        candidate = Path(shutil.which(argument) or argument) if index == 0 else Path(argument)
        if candidate.is_file():
            launcher.update(candidate.name.encode() + b'\0')
            launcher.update(digest(candidate.resolve())[0].encode())
        else:
            launcher.update(argument.encode() + b'\0')
    implementation = sha256()
    package = Path(__file__).resolve().parents[1]
    for path in sorted(package.rglob('*.py')):
        implementation.update(path.relative_to(package).as_posix().encode() + b'\0')
        implementation.update(path.read_bytes())
    with store.connect() as db:
        version = db.execute('SELECT version FROM metadata').fetchone()[0]
    validate_indexes(home, native_id)
    return {'agentbridge_digest': implementation.hexdigest(), 'native_digest': launcher.hexdigest(),
            'store_schema': version, 'native_schema': NATIVE_SCHEMA}


def sqlite_copy(source, target):
    # The source is already type/path-checked. Use SQLite's online backup, never raw pages.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as origin:
        with closing(sqlite3.connect(target)) as destination, destination:
            deadline = time.monotonic() + 30

            def bounded(status, remaining, total):
                if time.monotonic() > deadline:
                    fail('checkpoint_busy')

            origin.backup(destination, pages=128, progress=bounded, sleep=.01)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                fail('checkpoint_corrupt')
    target.chmod(0o600)


def materialize(home, stage):
    files, total = [], 0
    for relative, source in inventory(home):
        target = stage / relative
        private_directory(target.parent)
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
            is_sqlite = handle.read(16) == b'SQLite format 3\0'
        if is_sqlite:
            sqlite_copy(source, target)
            kind = 'native_index'
        else:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'wb') as out, os.fdopen(source_fd, 'rb') as inp:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            kind = 'native_state' if relative.endswith('.jsonl') else 'native_metadata'
        checksum, size = digest(target)
        total += size
        if total > MAX_BYTES:
            fail('checkpoint_too_large')
        files.append({'path': relative, 'kind': kind, 'bytes': size, 'sha256': checksum})
    if not files:
        fail('checkpoint_incomplete')
    return files


def validate_indexes(home, native_id):
    for path in sorted(Path(home).glob('state_*.sqlite')):
        if path.name != 'state_5.sqlite':
            fail()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            names = {column[1] for column in db.execute('PRAGMA table_info(threads)')}
            if not {'id', 'rollout_path'} <= names:
                fail()
            rows = db.execute('SELECT id FROM threads').fetchall()
            if (native_id,) not in rows:
                fail('checkpoint_scope_mismatch')
            for (thread_id,) in rows:
                canonical_uuid(thread_id)
                locate('codex', home, thread_id)


def relocate_indexes(home, native_id, *, target_home, workspace=None):
    """Only the reviewed Codex threads schema can rewrite a rollout path for a new root."""
    validate_indexes(home, native_id)
    for path in sorted(Path(home).glob('state_*.sqlite')):
        with closing(sqlite3.connect(path)) as db, db:
            rows = db.execute('SELECT id FROM threads').fetchall()
            for (thread_id,) in rows:
                rollout = locate('codex', home, thread_id)
                relocated = Path(target_home) / rollout.relative_to(home)
                db.execute('UPDATE threads SET rollout_path=? WHERE id=?',
                           (str(relocated), thread_id))
            if workspace:
                from .workspaces import relocate_index

                relocate_index(db, workspace)
