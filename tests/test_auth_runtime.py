"""One proxy login survives process restarts without a native auth worker."""

import io
import json
from pathlib import Path
import secrets
import shutil
import sys

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.rpc import rpc
from test_proxy_management import EMPTY_CONFIG, local_management


FIXTURE = Path(__file__).parent / 'fixtures' / 'test_grantbridge_adapter.py'


@pytest.fixture
def authentication(tmp_path, monkeypatch):
    management_key = secrets.token_hex(24)
    proxy_key = secrets.token_hex(24)
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', management_key)
    monkeypatch.setenv('LAB_PROXY_KEY', proxy_key)
    adapter = tmp_path / 'grantbridge' / 'scripts' / 'agentbridge-adapter.mjs'
    adapter.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, adapter)
    monkeypatch.setenv('AGENTBRIDGE_GRANTBRIDGE_ROOT', str(adapter.parent.parent))
    monkeypatch.setenv('AGENTBRIDGE_NODE', sys.executable)
    responses = {
        '/v0/management/auth-files': (200, {'files': []}, {}),
        '/v0/management/config': (200, EMPTY_CONFIG, {}),
        '/v0/management/auth-files/models?name=one.json': (
            200, {'models': [{'id': 'fixture/model'}]}, {}),
    }
    with local_management(responses) as (port, _):
        yield (tmp_path / 'bridge', tmp_path / 'adapter-state', responses,
               f'http://127.0.0.1:{port}/v1', management_key, proxy_key)


def options(auth, *, request_key='once'):
    _, adapter_state, _, base_url, _, _ = auth
    return {'provider': 'codex', 'name': 'Fixture', 'proxy_base_url': base_url,
            'key_env': 'LAB_PROXY_KEY', 'management_key_env': 'LAB_MANAGEMENT_KEY',
            'data_dir': adapter_state, 'owner_ref': 'fixture-owner',
            'request_key': request_key}


def authorize(auth):
    _, adapter_state, responses, _, _, _ = auth
    (adapter_state / 'authorize').touch()
    responses['/v0/management/auth-files'] = (200, {'files': [{
        'name': 'one.json', 'source': 'file', 'runtime_only': False,
        'provider': 'codex', 'status': 'active', 'disabled': False,
        'unavailable': False, 'auth_index': 'fixture-auth-index',
        'account_type': 'oauth', 'email': 'person@example.test',
        'id_token': {'chatgpt_account_id': 'fixture-account-id'},
        'cooldowns': [],
    }]}, {})


def complete(bridge, auth, attempt):
    authorize(auth)
    assert bridge.account_login_status(
        attempt['attempt_id'], owner_ref=attempt['owner_ref'])['status'] == 'authorized'
    assert bridge.account_login_check(
        attempt['attempt_id'], owner_ref=attempt['owner_ref'])['status'] == 'verified'
    return bridge.account_login_complete(attempt['attempt_id'], owner_ref=attempt['owner_ref'])


def test_proxy_login_is_restart_safe_and_idempotent(authentication):
    root, adapter_state, _, base_url, management_key, proxy_key = authentication
    with Bridge(root) as bridge:
        started = bridge.account_login_start(**options(authentication))
        assert started['status'] == 'awaiting_user'
        assert bridge.account_login_start(**options(authentication))['attempt_id'] == started['attempt_id']
        assert bridge.accounts() == []
        with pytest.raises(BridgeError) as error:
            bridge.account_login_complete(started['attempt_id'], owner_ref=started['owner_ref'])
        assert error.value.code == 'authentication_not_verified'

    with Bridge(root) as resumed:
        result = complete(resumed, authentication, started)
        account = resumed.resolve_account('Fixture')
        assert result['attempt']['status'] == 'bound'
        assert account.engine == 'codex' and account.provider == 'codex'
        assert account.home is None and account.proxy_base_url == base_url
        assert account.supported_models == ('fixture/model',)
        assert resumed.account_login_complete(
            started['attempt_id'], owner_ref=started['owner_ref']) == result
        assert resumed.account_login_start(**options(authentication))['attempt_id'] == started['attempt_id']
        assert len(resumed.accounts()) == 1

    with Bridge(root) as another_process:
        assert another_process.account_login_status(
            started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'bound'
    for path in (*root.rglob('*'), *adapter_state.rglob('*')):
        if path.is_file():
            assert management_key.encode() not in path.read_bytes()
            assert proxy_key.encode() not in path.read_bytes()


def test_proxy_cancel_is_durable_and_never_binds_an_account(authentication):
    root, adapter_state, _, _, _, _ = authentication
    with Bridge(root) as bridge:
        started = bridge.account_login_start(**options(authentication))
        cancelled = bridge.account_login_cancel(
            started['attempt_id'], owner_ref=started['owner_ref'])
        assert cancelled['status'] == 'cancelled'
    with Bridge(root) as resumed:
        assert resumed.account_login_status(
            started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'cancelled'
        assert resumed.account_login_start(**options(authentication))['status'] == 'cancelled'
        with pytest.raises(BridgeError) as error:
            resumed.account_login_complete(started['attempt_id'], owner_ref=started['owner_ref'])
        assert error.value.code == 'authentication_not_verified'
        assert resumed.accounts() == []
    assert json.loads((adapter_state / 'proxy-attempt.json').read_text())['status'] == 'cancelled'


def test_completed_oauth_cannot_be_cancelled_without_discarding_credential(authentication):
    root, _, _, _, _, _ = authentication
    with Bridge(root) as bridge:
        started = bridge.account_login_start(**options(authentication))
        authorize(authentication)
        checked = bridge.account_login_check(
            started['attempt_id'], owner_ref=started['owner_ref'])
        assert checked['status'] == 'verified'
        with pytest.raises(BridgeError) as error:
            bridge.account_login_cancel(started['attempt_id'], owner_ref=started['owner_ref'])
        assert error.value.code == 'already_finished'
    with Bridge(root) as resumed:
        assert resumed.account_login_complete(
            started['attempt_id'], owner_ref=started['owner_ref'])['attempt']['status'] == 'bound'
        assert len(resumed.accounts()) == 1


def test_proxy_login_owner_and_request_key_scope_are_enforced(authentication):
    root, _, _, _, _, _ = authentication
    with Bridge(root) as bridge:
        started = bridge.account_login_start(**options(authentication))
        with pytest.raises(BridgeError) as error:
            bridge.account_login_status(started['attempt_id'], owner_ref='another-owner')
        assert error.value.code == 'authentication_attempt_not_found'
        with pytest.raises(BridgeError) as error:
            bridge.account_login_start(**{**options(authentication), 'key_env': 'OTHER_PROXY_KEY'})
        assert error.value.code == 'idempotency_conflict'
        out = io.StringIO()
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'accounts.list'}
        rpc(bridge, io.StringIO(json.dumps(request) + '\n'), out)
        assert json.loads(out.getvalue())['result'] == []


def test_reauthentication_cannot_replace_an_existing_proxy_identity(authentication):
    root, _, responses, _, _, _ = authentication
    with Bridge(root) as bridge:
        started = bridge.account_login_start(**options(authentication))
        account = complete(bridge, authentication, started)['account']
        original = bridge.resolve_account('Fixture')
        entry = responses['/v0/management/auth-files'][1]['files'][0]
        entry['id_token']['chatgpt_account_id'] = 'another-private-account'
        with pytest.raises(BridgeError) as error:
            bridge.account_login_start(**options(authentication, request_key='new-login'))
        assert error.value.code == 'identity_changed'
        assert bridge.resolve_account('Fixture') == original
        assert bridge.resolve_account('Fixture').id == account['id']
        assert len(bridge.accounts()) == 1
