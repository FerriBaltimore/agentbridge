"""Trusted destination workspace binding, separate from historical transcript contents."""

import os
from pathlib import Path

from ..workspace_policy import validate_execution_workspace
from .native import fail


def rebind(db, destination_root, workspaces):
    if workspaces is None:
        return
    if not isinstance(workspaces, dict):
        fail('checkpoint_invalid')
    for instance_id, value in workspaces.items():
        session = db.execute('SELECT cwd FROM sessions WHERE id=?', (instance_id,)).fetchone()
        if not session or not isinstance(value, (str, Path)):
            fail('checkpoint_scope_mismatch')
        target = Path(value).absolute()
        if (target != target.resolve() or not target.is_dir()
                or target.stat().st_uid != os.getuid() or target.stat().st_mode & 0o077):
            fail('checkpoint_unsafe_path')
        validate_execution_workspace(str(target), destination_root, workspace_write=True)
        db.execute('INSERT OR REPLACE INTO recovery_workspaces VALUES (?,?,?)',
                   (instance_id, session['cwd'], str(target)))
        db.execute('UPDATE sessions SET cwd=? WHERE id=?', (str(target), instance_id))


def relocate_index(db, workspace):
    columns = {column[1] for column in db.execute('PRAGMA table_info(threads)')}
    if 'cwd' not in columns:
        return
    source, target = Path(workspace['source_path']), Path(workspace['target_path'])
    for native_id, cwd in db.execute('SELECT id,cwd FROM threads').fetchall():
        path = Path(cwd)
        if '..' in path.parts or not path.is_relative_to(source):
            fail('checkpoint_scope_mismatch')
        db.execute('UPDATE threads SET cwd=? WHERE id=?',
                   (str(target / path.relative_to(source)), native_id))
