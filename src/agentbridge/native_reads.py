"""Native thread queries use its live owner, or an isolated read-only connection."""

from dataclasses import asdict
import os
from tempfile import TemporaryDirectory

from .codex_control import CodexControl
from .checkpoint.content import instance_lock
from .errors import BridgeError
from .execution_context import codex_config
from .models import RunOptions
from .native_control import request
from .native_observations import thread
from .native_sessions import require_native_session
from .process import alive
from .provider_channel import ProviderChannel
from .provider_contracts import ContractRegistry
from .proxy import session_home
from .security import Redactor, base_environment
from .subprocess_path import python_path
from .transports import command


def read(bridge, instance_id, *, include_turns=True, reopen=False):
    if type(include_turns) is not bool:
        raise BridgeError('invalid_request', 'include_turns must be a boolean.')
    with instance_lock(bridge.store, instance_id, busy_code='busy'):
        return _read(bridge, instance_id, include_turns, reopen)


def _read(bridge, instance_id, include_turns, reopen):
    session = bridge.get_session(instance_id)
    with bridge.store.connect() as db:
        native_id = require_native_session(db, session)
        # Native snapshots have no shared cursor with notifications. Replay the
        # entire latest turn so clients can rebuild observed items independently
        # from snapshot text. Never append replay deltas to snapshot text.
        latest = db.execute('SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
                            (instance_id,)).fetchone()
        after_seq = (db.execute('SELECT COALESCE(MIN(seq),1)-1 FROM events WHERE run_id=?',
                                (latest['id'],)).fetchone()[0] if latest else 0)
        from .checkpoint import state
        cursor = state.cursor(db, after_seq) if state.identity(db)['enabled'] else None
    if native_id is None:
        raise BridgeError('native_session_missing', 'This instance has no native thread yet.')
    previous = bridge.store.last_session_run(instance_id)
    running = previous and (previous['state'] in {'starting', 'running', 'stopping'}
                            or alive(previous['worker_pid'], previous['worker_identity'])
                            or alive(previous['child_pid'], previous['child_identity']))
    if running:
        observed = request(bridge.store, previous, 'reopen' if reopen else 'read',
                           include_turns=include_turns)
    else:
        observed = _disconnected_read(bridge, session, include_turns, reopen)
    if observed['native_session_id'] != native_id:
        raise BridgeError('native_session_diverged', 'The native thread identity changed.')
    result = {**observed, 'instance_id': instance_id, 'after_seq': after_seq,
              'replay_mode': 'replace_observed_items'}
    if cursor is not None:
        result['cursor'] = cursor
    return result


def _disconnected_read(bridge, session, include_turns, reopen):
    home = bridge.root / 'codex-runtime' / session['id']
    if not home.is_dir() or home.is_symlink() or home.resolve() != home:
        raise BridgeError('native_history_unavailable',
                          'The original native history directory is unavailable.')
    account = bridge.account(session['account_id'])
    ContractRegistry(bridge.store).check(account, enforce=True)
    options = RunOptions(steerable=True, timeout=5, sandbox='read-only', permission_mode='dontAsk')
    env = base_environment()
    env['PYTHONPATH'] = python_path()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # Native reads never call login, quota discovery or the provider. Preserve only the
    # selected proxy credential if available; never pass the management credential.
    secret = os.environ.get(account.key_env, '') if account.key_env else ''
    if secret:
        env[account.key_env] = secret
    env['CODEX_HOME'] = str(session_home(bridge.root, session['id']))
    env['HOME'] = env['CODEX_HOME']
    redactor = Redactor((secret,))
    with TemporaryDirectory(prefix='agentbridge-native-read-') as temporary:
        env['TMPDIR'] = temporary
        argv = command(account, session, options, native_transport=True, state_root=bridge.root)
        with ProviderChannel(argv, cwd=session['cwd'], env=env) as channel:
            payload = {'options': asdict(options)}
            control = CodexControl(channel, payload, lambda _: None, None)
            control.thread_id = session['native_id']
            control.rpc('initialize', {'clientInfo': {'name': 'agentbridge', 'version': '2.11.2'}})
            channel.send({'method': 'initialized', 'params': {}})
            # A fresh app-server has no loaded thread. Resume is a non-executing
            # load of this exact ID, and distinguishes absence from "not loaded".
            config = codex_config(mcp_enabled=False, selected_context=True)
            resumed = control.rpc('thread/resume', {'threadId': session['native_id'],
                'cwd': session['cwd'], 'approvalPolicy': 'never', 'sandbox': 'read-only',
                'config': config})
            if resumed.get('thread', {}).get('id') != session['native_id']:
                raise BridgeError('native_session_diverged', 'The native thread identity changed.')
            result = control.rpc('thread/read', {'threadId': session['native_id'],
                                                  'includeTurns': include_turns})
            return redactor.clean(thread(result.get('thread')))
