"""Reopen an explicit native binding, including a lost local instance record."""

from pathlib import Path

from .checkpoint.content import instance_lock
from .checkpoint.state import require_admission
from .errors import BridgeError
from .models import identifier, TERMINAL
from .native_reads import read, _disconnected_read
from .native_sessions import bind_native_session, require_native_session, validate_native_id
from .process import alive
from .workspace_policy import validate_workspace


def reopen(bridge, instance_id, *, native_session_id=None, account_ref=None,
           workspace_path=None, model=None, include_turns=True):
    identifier(instance_id)
    if type(include_turns) is not bool:
        raise BridgeError('invalid_request', 'include_turns must be a boolean.')
    try:
        session = bridge.get_session(instance_id)
    except BridgeError as error:
        if error.code not in {'not_found', 'instance_not_found'}:
            raise
        _restore(bridge, instance_id, native_session_id, account_ref, workspace_path, model)
    else:
        if (native_session_id is not None and session['native_id'] is not None
                and native_session_id != session['native_id']):
            raise BridgeError('native_session_diverged', 'The native conversation binding differs.')
        if account_ref is not None and bridge.resolve_account(account_ref).id != session['account_id']:
            raise BridgeError('native_session_diverged', 'The account binding differs.')
        if ((workspace_path is not None and str(Path(workspace_path).resolve()) != session['cwd'])
                or (model is not None and model != session['model'])):
            raise BridgeError('native_session_diverged', 'The instance configuration differs.')
        if session['native_id'] is None and native_session_id is not None:
            _restore_native_id(bridge, session, native_session_id)
    return read(bridge, instance_id, include_turns=include_turns, reopen=True)


def _restore_native_id(bridge, session, native_id):
    validate_native_id(native_id)
    with instance_lock(bridge.store, session['id'], busy_code='busy'):
        with bridge.store.connect() as db:
            rows = db.execute('SELECT * FROM runs WHERE session_id=?',
                              (session['id'],)).fetchall()
            if any(row['state'] not in TERMINAL or any(
                    alive(row[p + '_pid'], row[p + '_identity']) for p in ('worker', 'child'))
                   for row in rows):
                raise BridgeError('native_connection_unavailable',
                                  'The existing execution must stop before metadata rebind.')
        proposed = {**session, 'native_id': native_id}
        observed = _disconnected_read(bridge, proposed, False, True)
        if observed['native_session_id'] != native_id:
            raise BridgeError('native_session_diverged', 'The native conversation binding differs.')
        with bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            require_admission(db, session['id'])
            bind_native_session(db, session['id'], native_id)
            require_native_session(db, proposed)


def _restore(bridge, instance_id, native_id, account_ref, workspace_path, model):
    if any(value is None for value in (native_id, account_ref, workspace_path, model)):
        raise BridgeError('native_binding_required',
                          'Reopening missing metadata requires the original native ID, account, '
                          'workspace and model.')
    validate_native_id(native_id)
    account = bridge.resolve_account(account_ref)
    workspace = str(validate_workspace(workspace_path, bridge.root))
    with instance_lock(bridge.store, instance_id, busy_code='busy'):
        with bridge.store.connect() as db:
            if db.execute('SELECT 1 FROM deleted_instances WHERE session_id=?',
                          (instance_id,)).fetchone():
                raise BridgeError('instance_deleted', 'A deleted instance cannot be recreated.')
            if db.execute('SELECT 1 FROM evaluation_instances WHERE session_id=?',
                          (instance_id,)).fetchone():
                raise BridgeError('evaluation_consumed', 'Evaluation instances cannot be reopened.')
            binding = db.execute('SELECT native_id FROM native_session_bindings WHERE session_id=?',
                                 (instance_id,)).fetchone()
            if binding and binding['native_id'] != native_id:
                raise BridgeError('native_session_diverged', 'The saved native binding differs.')
            runs = db.execute('SELECT * FROM runs WHERE session_id=? ORDER BY rowid DESC',
                              (instance_id,)).fetchall()
            if runs and runs[0]['account_id'] != account.id:
                raise BridgeError('native_session_diverged', 'The last execution used another account.')
            if any(row['state'] not in TERMINAL or any(
                    alive(row[p + '_pid'], row[p + '_identity']) for p in ('worker', 'child'))
                   for row in runs):
                raise BridgeError('native_connection_unavailable',
                                  'An existing execution must be recovered before metadata rebind.')
        home = bridge.root / 'codex-runtime' / instance_id
        if not home.is_dir() or home.is_symlink() or home.resolve() != home:
            raise BridgeError('native_history_unavailable',
                              'The original native history directory is unavailable.')
        session = {'id': instance_id, 'native_id': native_id, 'account_id': account.id,
                   'cwd': workspace, 'model': model}
        observed = _disconnected_read(bridge, session, False, True)
        if observed['native_session_id'] != native_id:
            raise BridgeError('native_session_diverged', 'The native conversation binding differs.')
        bridge.store.add_session(instance_id, account.id, workspace, model, native_id=native_id)
