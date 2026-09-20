"""Opt-in transfer of one native session. Credentials and configuration stay put.

Derived from Fullbrain native_move/resumable, source 1013ff18 (2026-09-18).
Layouts are version-dependent and remain subject to provider acceptance.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

from .errors import BridgeError, UnsupportedError
from .models import identifier


def safe_path(root, relative):
    root, relative = Path(root).resolve(), Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise BridgeError('unsafe_path', 'Session escaped its home.')
    path = root
    # Inspect unresolved components: resolving first erases evidence of links
    # whose target happens to remain inside the same native home.
    for component in relative.parts:
        path = path / component
        if path.is_symlink():
            raise BridgeError('unsafe_path', 'Session paths cannot contain symbolic links.')
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise BridgeError('unsafe_path', 'Session escaped its home.')
    return resolved


def locate(engine,home,native_id):
    identifier(native_id)
    home=Path(home).resolve()
    patterns={'codex':f'sessions/**/rollout-*-{native_id}.jsonl','claude':f'projects/*/{native_id}.jsonl'}
    if engine not in patterns:raise UnsupportedError('This engine has no native file transfer.')
    matches=[safe_path(home,p.relative_to(home)) for p in home.glob(patterns[engine]) if p.is_file()]
    if len(matches)!=1:raise BridgeError('native_session_missing','Expected exactly one native session artifact.')
    return matches[0]


def version_tuple(text):
    if not isinstance(text, str) or len(text) > 4096:
        return None
    matches = re.findall(r'(?<![0-9A-Za-z_.+-])(\d+)\.(\d+)\.(\d+)(?![0-9A-Za-z_.+-])', text)
    return tuple(map(int, matches[0])) if len(matches) == 1 else None


def compatible(engine, path, command, native_id=None):
    """Validate a bounded local-layout observation, not provider migration support.

    Native JSONL and SQLite are private compatibility formats. Matching major
    versions do not demonstrate their compatibility, so cross-version copies
    must use portable continuity until separately verified.
    """
    recorded = None
    if engine not in {'codex', 'claude'}:
        return False
    with path.open('rb') as stream:
        for _ in range(100):
            line = stream.readline(1024 * 1024 + 1)
            if len(line) > 1024 * 1024:
                return False
            if not line:
                break
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(record, dict):
                continue
            if engine == 'codex':
                payload = record.get('payload')
                if record.get('type') != 'session_meta' or not isinstance(payload, dict):
                    continue
                if native_id is not None and payload.get('id') != native_id:
                    return False
                recorded = payload.get('cli_version')
            else:
                if 'version' not in record:
                    continue
                if native_id is not None and record.get('sessionId') != native_id:
                    return False
                recorded = record.get('version')
            if recorded is not None:
                break
    old = version_tuple(recorded)
    if old is None:
        return False
    try:
        result = subprocess.run([*command, '--version'], capture_output=True, text=True, timeout=5)
        new = version_tuple(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        new = None
    return old == new


def copy_session(engine,native_id,source_home,target_home,command):
    source=locate(engine,source_home,native_id)
    if not compatible(engine,source,command,native_id):
        raise BridgeError('native_version_unverified','Native copy requires a known compatible client version.')
    target=safe_path(target_home,source.relative_to(Path(source_home).resolve()))
    if source.resolve()==target.resolve():return native_id
    if target.exists():
        with source.open('rb') as a,target.open('rb') as b:
            while chunk:=b.read(1024*1024):
                if a.read(len(chunk))!=chunk:raise BridgeError('native_session_diverged','Destination session has diverged.')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,staged=tempfile.mkstemp(prefix='.moving-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as out,source.open('rb') as inp:
            while chunk:=inp.read(1024*1024):out.write(chunk)
            out.flush();os.fsync(out.fileno())
        if engine=='codex':index_copy(source_home,target_home,native_id,target)
        os.replace(staged,target)
    finally:
        if os.path.exists(staged):os.unlink(staged)
    return native_id


def _indexes(home):
    paths = [path for path in Path(home).glob('state_*.sqlite')
             if re.fullmatch(r'state_[0-9]+\.sqlite', path.name)]
    return sorted(paths, key=lambda path: int(path.stem.split('_')[1]))


def index_copy(source_home, target_home, id, target):
    databases = _indexes(target_home)
    if not databases:
        return  # Clients with no database build it from rollouts.
    sources = _indexes(source_home)
    if not sources:
        raise BridgeError('native_index_unverified', 'Source index row is unavailable; use portable transfer.')
    source = safe_path(source_home, sources[-1].name)
    destination = safe_path(target_home, databases[-1].name)
    try:
        # Never select an older index merely because the newest schema drifted.
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM threads WHERE id=?', (id,)).fetchone()
            if row is None:
                raise BridgeError('native_index_unverified', 'Source session metadata is unavailable.')
            template = dict(row)
        with sqlite3.connect(destination) as db:
            columns = db.execute('PRAGMA table_info(threads)').fetchall()
            names = [column[1] for column in columns]
            primary = [column[1] for column in columns if column[5]]
            if 'rollout_path' not in names or primary != ['id']:
                raise BridgeError('native_index_unverified', 'Unsupported destination index schema.')
            values = {key: value for key, value in template.items() if key in names}
            values['rollout_path'] = str(target)
            for _, name, _, required, default, _ in columns:
                if required and default is None and values.get(name) is None:
                    raise BridgeError('native_index_unverified', 'Destination requires unknown session metadata.')
            keys = list(values)
            quoted = ['"' + key.replace('"', '""') + '"' for key in keys]
            # REPLACE deletes the original row and can discard new optional
            # metadata or invalidate dependent records. Update known fields only.
            update = ','.join(f'{key}=excluded.{key}' for key in quoted if key != '"id"')
            db.execute(f'INSERT INTO threads ({",".join(quoted)}) VALUES ({",".join("?" for _ in keys)}) '
                       f'ON CONFLICT(id) DO UPDATE SET {update}', [values[key] for key in keys])
    except sqlite3.Error:
        raise BridgeError('native_index_unverified', 'The native session index schema is unsupported.') from None
