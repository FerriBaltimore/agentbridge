import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from agentbridge import Account, Bridge
from agentbridge.errors import BridgeError
from agentbridge.rpc import dispatch, rpc

FIXTURES = Path(__file__).parent / 'fixtures'


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.03)
    raise AssertionError('Authentication subprocess did not reach the expected state.')


@pytest.fixture
def authentication(tmp_path, monkeypatch):
    adapter = tmp_path / 'grantbridge' / 'scripts' / 'agentbridge-adapter.mjs'
    adapter.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / 'test_grantbridge_adapter.py', adapter)
    monkeypatch.setenv('AGENTBRIDGE_GRANTBRIDGE_ROOT', str(adapter.parent.parent))
    monkeypatch.setenv('AGENTBRIDGE_NODE', sys.executable)
    bridge = Bridge(tmp_path / 'bridge')
    native = tmp_path / 'native'
    yield bridge, native
    # No subprocess is left running after a test, including failures.
    with bridge.store.connect() as db:
        attempts = db.execute('SELECT id,owner FROM auth_attempts').fetchall()
    for row in attempts:
        bridge.account_login_cancel(row['id'], owner_ref=row['owner'])
        wait_for(lambda: not bridge.authentication.runtime.active(bridge.authentication.runtime.get(row['id'])))
    bridge.authentication.runtime.reap()


def start(bridge, native, engine='cursor', **extra):
    return bridge.account_login_start(engine=engine, name='Fixture', data_dir=native,
                                     owner_ref='fixture-owner', request_key='once', **extra)


def settle(bridge, attempt):
    wait_for(lambda: not bridge.authentication.runtime.active(
        bridge.authentication.runtime.get(attempt['attempt_id'])))
    return bridge.account_login_status(attempt['attempt_id'], owner_ref=attempt['owner_ref'])


def verify(bridge, native, attempt):
    wait_for(lambda: (native / 'attempt.json').exists())
    (native / 'authorize').touch()
    assert settle(bridge, attempt)['status'] == 'authorized'
    checked = bridge.account_login_check(attempt['attempt_id'], owner_ref=attempt['owner_ref'])
    assert checked['checking']
    assert settle(bridge, attempt)['status'] == 'verified'


def test_start_survives_caller_exit_and_replays_without_new_provider(authentication):
    bridge, native = authentication
    script = ('import json,sys; from agentbridge import Bridge; '
              'print(json.dumps(Bridge(sys.argv[1]).account_login_start(engine="cursor", '
              'name="Fixture",data_dir=sys.argv[2],owner_ref="fixture-owner",request_key="once")))')
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')}
    result = subprocess.run([sys.executable, '-c', script, str(bridge.root), str(native)],
                            capture_output=True, text=True, env=env, timeout=5, check=True)
    attempt = json.loads(result.stdout)
    assert attempt['status'] == 'starting'
    wait_for(lambda: (native / 'attempt.json').exists())
    replay = start(bridge, native)
    assert replay['attempt_id'] == attempt['attempt_id']
    assert not bridge.accounts()
    with pytest.raises(BridgeError):
        bridge.account_login_complete(attempt['attempt_id'], owner_ref=attempt['owner_ref'])
    verify(bridge, native, attempt)
    complete = bridge.account_login_complete(attempt['attempt_id'], owner_ref=attempt['owner_ref'])
    assert complete['attempt']['status'] == 'bound'
    again = Bridge(bridge.root)
    assert again.account_login_status(attempt['attempt_id'], owner_ref=attempt['owner_ref'])['status'] == 'bound'
    assert again.account_login_complete(attempt['attempt_id'], owner_ref=attempt['owner_ref']) == complete
    assert start(again, native)['attempt_id'] == attempt['attempt_id']
    assert len(again.accounts()) == 1


def test_cursor_vault_reference_reaches_worker_and_is_never_persisted(authentication, tmp_path):
    bridge, native = authentication
    bridge.register(Account('cursor-fixture', 'cursor', name='Fixture',
        command=(sys.executable, str(FIXTURES / 'test_cursor_sdk_provider.py'))))
    attempt = start(bridge, native)
    verify(bridge, native, attempt)
    result = bridge.account_login_complete(attempt['attempt_id'], owner_ref=attempt['owner_ref'])
    assert result['account']['credential_ref']['attempt_id'] == 'fixture-attempt'
    instance = bridge.instance_create(account_ref='Fixture', workspace_path=str(tmp_path), model='fixture-model')
    accepted = bridge.message_create(instance['id'], 'hello', idempotency_key='input-once')
    run = bridge.run(accepted['turn_id'])
    assert run.wait(8)['state'] == 'completed'
    assert 'fixture-key-never-real' not in str(list(run.events()))
    for file in bridge.root.iterdir():
        if file.is_file():
            assert b'fixture-key-never-real' not in file.read_bytes()
    accounts = dispatch(bridge, 'accounts.list', {})
    assert 'credential_ref' not in str(accounts)
    assert 'adapter' not in str(accounts)
    (native / 'expired').touch()
    replay = bridge.message_create(instance['id'], 'hello', idempotency_key='input-once')
    assert replay['turn_id'] == accepted['turn_id'] and replay['replayed']
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance['id'], 'must not launch')
    assert error.value.code == 'credential_expired'
    assert len(bridge.runs()) == 1
    status = bridge.account_status(account_ref='Fixture', refresh=True)
    assert status['authentication']['status'] == 'authentication_required'
    assert status['reason'] == 'credential_expired'
    bridge.close()


def test_cancel_during_start_is_durable_and_never_creates_account(authentication):
    bridge, native = authentication
    attempt = start(bridge, native)
    bridge.account_login_cancel(attempt['attempt_id'], owner_ref=attempt['owner_ref'])
    assert settle(bridge, attempt)['status'] == 'cancelled'
    assert not bridge.accounts()


def test_cancel_during_verification_cannot_be_resurrected(authentication):
    bridge, native = authentication
    attempt = start(bridge, native)
    wait_for(lambda: (native / 'attempt.json').exists())
    (native / 'authorize').touch()
    settle(bridge, attempt)
    bridge.account_login_check(attempt['attempt_id'], owner_ref=attempt['owner_ref'])
    assert bridge.account_login_cancel(attempt['attempt_id'], owner_ref=attempt['owner_ref'])['status'] == 'cancelled'
    assert settle(bridge, attempt)['status'] == 'cancelled'
    assert not bridge.accounts()


def test_login_owner_and_public_transport_cannot_be_overridden(authentication):
    bridge, native = authentication
    attempt = start(bridge, native)
    with pytest.raises(BridgeError) as error:
        bridge.account_login_status(attempt['attempt_id'], owner_ref='another-owner')
    assert error.value.code == 'authentication_attempt_not_found'
    with pytest.raises(BridgeError) as error:
        dispatch(bridge, 'accounts.login.start', {'engine': 'cursor', 'name': 'X', 'grantbridge_root': '/unexpected'})
    assert error.value.code == 'unsupported_parameter'


def test_rpc_remains_available_while_login_is_pending(authentication):
    bridge, native = authentication
    attempt = start(bridge, native)
    out = io.StringIO()
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'accounts.list'}
    rpc(bridge, io.StringIO(json.dumps(request) + '\n'), out)
    assert json.loads(out.getvalue())['result'] == []
    assert bridge.authentication.runtime.active(bridge.authentication.runtime.get(attempt['attempt_id']))
