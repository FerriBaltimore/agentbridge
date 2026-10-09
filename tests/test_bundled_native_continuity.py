"""Offline acceptance: real reviewed Codex, deterministic loopback Responses."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

import pytest

from agentbridge import Account, Bridge, BridgeError, RunOptions
from agentbridge.bundle.runtime import PACKAGE_ROOT
from agentbridge.codex_control import CodexControl
from agentbridge.native_sandbox import available
from agentbridge.provider_channel import ProviderChannel
from agentbridge.security import base_environment
from agentbridge.transports import command
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from fixtures.test_responses_server import responses_server


_USER_NAMESPACE_PROBE = '''
import os
uid, gid = os.getuid(), os.getgid()
os.unshare(os.CLONE_NEWUSER)
for name, value in (('setgroups', 'deny'), ('uid_map', f'{uid} {uid} 1'), ('gid_map', f'{gid} {gid} 1')):
    with open('/proc/self/' + name, 'w') as stream:
        stream.write(value)
'''


def unprivileged_user_namespace_permitted():
    """Probe the host policy every restricted native launch depends on.

    The bundled bubblewrap runs unconfined and maps its own identity in a fresh
    user namespace. Hosts enforcing AppArmor's unprivileged user namespace
    restriction confine that process as ``unprivileged_userns`` and deny the
    mapping, so the SDK fails closed by design. A caller already running inside
    a profile which grants ``userns`` passes the permission on to these tests.
    """
    if not hasattr(os, 'unshare'):
        return True
    try:
        return subprocess.run([sys.executable, '-I', '-c', _USER_NAMESPACE_PROBE],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not (PACKAGE_ROOT / 'assets/codex.tar.gz').is_file() or not available()
    or not unprivileged_user_namespace_permitted(),
    reason='The bundled Codex archive, Landlock ABI 5 and a host which lets an unconfined '
           'process create user namespaces are required; see native-codex-acceptance.md.',
)


def native_history(bridge, instance_id, env):
    """Ask the actual native index API to find and read the same durable thread."""
    session = bridge.get_session(instance_id)
    account = bridge.account(session['account_id'])
    home = bridge.root / 'codex-runtime' / instance_id
    env = {key: value for key, value in env.items() if not key.startswith('FIXTURE_NATIVE_MANAGEMENT_')}
    env.update({'HOME': str(home), 'CODEX_HOME': str(home)})
    argv = command(account, session, RunOptions(permission_mode='default'),
                   native_transport=True, state_root=bridge.root)
    with TemporaryDirectory(prefix='fixture-native-read-') as temporary:
        env['TMPDIR'] = temporary
        with ProviderChannel(argv, cwd=session['cwd'], env=env) as channel:
            control = CodexControl(channel, {'options': {'timeout': 10}}, lambda _: None, None)
            control.rpc('initialize', {'clientInfo': {'name': 'fixture_native_index', 'version': '1'}})
            channel.send({'method': 'initialized', 'params': {}})
            listing = control.rpc('thread/list', {'limit': 100, 'sourceKinds': ['exec', 'appServer', 'vscode', 'cli'],
                                                   'modelProviders': []})
            history = control.rpc('thread/read', {'threadId': session['native_id'], 'includeTurns': True})
    return listing, history


@pytest.mark.parametrize('permission_mode', ['dontAsk', 'default'], ids=['exec', 'app-server'])
def test_real_codex_preserves_indexed_history_across_proxy_changes(tmp_path, monkeypatch, permission_mode):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    state = tmp_path / 'state'
    native_env = base_environment()
    native_env['PYTHONPATH'] = str(PACKAGE_ROOT.parents[1])
    all_observations = []
    with ExitStack() as stack:
        bridge = Bridge(state)
        stack.callback(bridge.close, cancel=True)
        routes = [('alpha', 'codex', ('fixture-model-a', 'fixture-model-b')),
                  ('beta', 'claude', ('fixture-model-c',))]
        for account_id, provider, models in routes:
            endpoint, observed = stack.enter_context(responses_server(account_id, provider, models))
            all_observations.append(observed)
            key_env = f'FIXTURE_NATIVE_KEY_{account_id.upper()}'
            management_env = f'FIXTURE_NATIVE_MANAGEMENT_{account_id.upper()}'
            monkeypatch.setenv(key_env, f'fixture-client-{account_id}')
            monkeypatch.setenv(management_env, f'fixture-management-{account_id}')
            native_env[key_env] = f'fixture-client-{account_id}'
            native_env[management_env] = f'fixture-management-{account_id}'
            seed_authenticated_proxy_account(bridge.store, Account(
                account_id, 'codex', provider=provider, supported_models=models,
                proxy_base_url=endpoint, key_env=key_env, management_key_env=management_env),
                observe_local=True)
        instance = bridge.instance_create(model='fixture-model-a', account_ref='alpha',
                                          routing_mode='automatic', workspace_path=workspace)
        instance_id = instance['id']
        native_ids = []
        child_pids = []
        for index in range(3):
            if index == 1:
                bridge.instance_update(instance_id, model='fixture-model-b')
            if index == 2:
                bridge.instance_update(instance_id, model='fixture-model-c', provider='claude')
                bridge.close()
                driver = Path(__file__).parent / 'fixtures/test_native_session_driver.py'
                child = subprocess.run([sys.executable, '-P', str(driver)], input=json.dumps({
                    'state': str(state), 'instance_id': instance_id, 'prompt': 'fixture request 3',
                    'permission_mode': permission_mode}), env=native_env,
                    capture_output=True, text=True, timeout=45, check=True)
                result = json.loads(child.stdout)
                bridge = Bridge(state)
                stack.callback(bridge.close, cancel=True)
            else:
                turn = bridge.message_create(instance_id, f'fixture request {index + 1}',
                                             permission_mode=permission_mode, timeout_ms=30000)
                run = bridge.run(turn['turn_id'])
                result = {**run.wait(40), 'text': run.text}
            assert result['state'] == 'completed', result
            assert result['text'] == f'fixture answer {index + 1} via ' + ('beta' if index == 2 else 'alpha')
            native_ids.append(bridge.instance_get(instance_id)['native_session_id'])
            child_pids.append(result['child_pid'])
        assert len(set(native_ids)) == 1
        assert len(set(child_pids)) == 3
        observations = all_observations[0] + all_observations[1]
        assert [item['model'] for item in observations] == [
            'fixture-model-a', 'fixture-model-b', 'fixture-model-c']
        for index, observed in enumerate(observations):
            assert observed['prompts'] == [f'fixture request {number + 1}' for number in range(index + 1)]
            assert observed['answers'] == [f'fixture answer {number + 1} via alpha' for number in range(index)]
        home = state / 'codex-runtime' / instance_id
        rollouts = list((home / 'sessions').rglob('*.jsonl'))
        assert len(rollouts) == 1
        metadata = json.loads(rollouts[0].read_text().splitlines()[0])
        assert metadata['payload']['id'] == native_ids[0]
        assert metadata['payload']['cli_version'] == '0.153.0'
        databases = list(home.glob('state_*.sqlite'))
        assert len(databases) == 1
        with sqlite3.connect(databases[0]) as native_db:
            rows = native_db.execute('SELECT id,rollout_path FROM threads').fetchall()
        assert rows == [(native_ids[0], str(rollouts[0]))]
        listing, history = native_history(bridge, instance_id, native_env)
        assert [item['id'] for item in listing['data']] == [native_ids[0]]
        assert history['thread']['id'] == native_ids[0]
        assert len(history['thread']['turns']) == 3
        public = bridge.instance_read(instance_id)
        assert public['source'] == 'native'
        assert public['native_session_id'] == native_ids[0]
        assert len(public['turns']) == 3
        assert all(turn['status'] == 'completed' for turn in public['turns'])
        assert {item['text'] for turn in public['turns'] for item in turn['items']
                if item['type'] == 'agent_message'} == {
                    'fixture answer 1 via alpha', 'fixture answer 2 via alpha',
                    'fixture answer 3 via beta'}
        reopened = bridge.instance_reopen(instance_id, native_session_id=native_ids[0])
        assert reopened['turns'] == public['turns']
        assert len(all_observations[0]) + len(all_observations[1]) == 3
        rollouts[0].unlink()
        with pytest.raises(BridgeError) as absent:
            bridge.instance_reopen(instance_id)
        assert absent.value.code == 'native_thread_missing'
        accepted = bridge.message_create(instance_id, 'fixture request missing history',
                                          permission_mode=permission_mode, timeout_ms=10000)
        missing = bridge.run(accepted['turn_id'])
        outcome = missing.wait(15)
        assert outcome['state'] != 'completed', outcome
        assert bridge.instance_get(instance_id)['native_session_id'] == native_ids[0]
        assert len(all_observations[0]) + len(all_observations[1]) == 3
        assert list((home / 'sessions').rglob('*.jsonl')) == []
