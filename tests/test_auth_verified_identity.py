"""Verified identity survives public reads and safe recovery of older attempt records."""

from copy import deepcopy

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.rpc import dispatch
from test_proxy_authentication import FakeProxyGrantBridge, proxy_responses, use_fake_grantbridge
from test_proxy_management import local_management


@pytest.fixture
def verified_login(tmp_path, monkeypatch):
    responses = proxy_responses()
    with local_management(responses) as (port, seen):
        monkeypatch.setenv('LAB_MANAGEMENT_KEY', 'fixture-management-key')
        monkeypatch.setenv('LAB_PROXY_KEY', 'fixture-client-key')
        with Bridge(tmp_path / 'state') as bridge:
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses))
            started = bridge.account_login_start(
                provider='codex', name='Fixture', email='PERSON@example.test',
                proxy_base_url=f'http://127.0.0.1:{port}/v1',
                key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY')
            binding = {'attempt_id': started['attempt_id'], 'owner_ref': started['owner_ref']}
            checked = dispatch(bridge, 'accounts.login.check', binding)
            yield bridge, binding, checked, responses, seen


def test_check_publishes_observed_identity_before_activation(verified_login):
    bridge, binding, checked, _, _ = verified_login
    assert checked['status'] == 'verified'
    assert checked['identity'] == {'email': 'person@example.test'}
    assert checked['verification'] == {'proxyBinding': 'passed'}
    assert bridge.accounts() == []
    assert dispatch(bridge, 'accounts.login.status', binding) == checked
    assert dispatch(bridge, 'accounts.login.check', binding) == checked
    completed = dispatch(bridge, 'accounts.login.complete', binding)
    assert completed['attempt']['status'] == 'bound'
    assert completed['identity'] == checked['identity']


def legacy_record(bridge, binding):
    row = bridge.store.get_auth_attempt(binding['attempt_id'], binding['owner_ref'])
    data = {key: value for key, value in row['data'].items() if key != 'identity'}
    return bridge.store.update_auth_attempt(row['id'], row['owner'], status='verified', data=data)


@pytest.mark.parametrize('method', ['accounts.login.status', 'accounts.login.check'])
def test_legacy_verified_identity_is_recovered_from_matching_management_evidence(
    verified_login, method,
):
    bridge, binding, _, responses, seen = verified_login
    before = legacy_record(bridge, binding)
    credentials = deepcopy(responses)
    previous_reads = len(seen)
    with pytest.raises(BridgeError) as foreign:
        dispatch(bridge, method, {**binding, 'owner_ref': 'foreign-owner'})
    assert foreign.value.code == 'authentication_attempt_not_found'
    assert len(seen) == previous_reads
    recovered = dispatch(bridge, method, binding)
    assert recovered['status'] == 'verified'
    assert recovered['identity'] == {'email': 'person@example.test'}
    assert len(seen) == previous_reads + 3
    saved = bridge.store.get_auth_attempt(binding['attempt_id'], binding['owner_ref'])
    assert saved['data']['proxy_binding'] == before['data']['proxy_binding']
    assert responses == credentials and bridge.accounts() == []
    assert dispatch(bridge, 'accounts.login.status', binding) == recovered
    assert len(seen) == previous_reads + 3


@pytest.mark.parametrize(('change', 'expected'), [
    ('email', 'identity_changed'), ('binding', 'proxy_binding_changed'),
    ('identity', 'proxy_binding_changed'),
])
def test_legacy_identity_recovery_never_overwrites_changed_evidence(
    verified_login, change, expected,
):
    bridge, binding, _, responses, _ = verified_login
    before = legacy_record(bridge, binding)
    credential = responses['/v0/management/auth-files'][1]['files'][0]
    if change == 'email':
        credential['email'] = 'different@example.test'
    elif change == 'binding':
        credential['auth_index'] = 'different-binding'
    else:
        credential['id_token']['chatgpt_account_id'] = 'different-identity'
    credentials = deepcopy(responses)
    with pytest.raises(BridgeError) as error:
        dispatch(bridge, 'accounts.login.status', binding)
    assert error.value.code == expected
    assert bridge.store.get_auth_attempt(binding['attempt_id'], binding['owner_ref']) == before
    assert responses == credentials and bridge.accounts() == []
