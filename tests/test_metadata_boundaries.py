"""Contract drift must not invent provider success, identity or compatible versions."""
import json
from types import SimpleNamespace

import pytest

from agentbridge import Account, Bridge, BridgeError, RunOptions
from agentbridge.auth_contract import attempt, response_result
from agentbridge.authentication import AuthenticationService
from agentbridge.error_observer import provider_version


@pytest.mark.parametrize('remote', [None, {}, {'status': 'verified'}, {'status': 'bound'},
    {'status': 'future-success'}, {'status': []}, {'status': 'authorized', 'checking': 0},
    {'status': 'authorized', 'verification': []}])
def test_unknown_auth_states_never_inherit_previous_verification(remote):
    with pytest.raises(BridgeError) as error:
        AuthenticationService._status(remote, 'verified')
    assert error.value.code == 'provider_protocol_error'


def test_additive_auth_fields_cannot_enter_public_or_persisted_projection(tmp_path):
    remote = {'id': 'remote', 'provider': 'codex', 'status': 'authorized',
        'identity': {'email': 'fixture@example.test', 'future_secret': 'private-credential'},
        'verification': {'freshProcess': 'passed', 'future_secret': 'private-credential'},
        'error': {'code': 'private-error-code', 'message': 'private-credential'},
        'new_field': {'secret': 'private-credential'}}
    value = attempt(remote, engine='codex')
    assert AuthenticationService._status(value) == 'verified'
    assert value['identity'] == {'email': 'fixture@example.test'}
    assert 'private' not in json.dumps(value)
    bridge = Bridge(tmp_path)
    row = {'id': 'local', 'owner': 'owner', 'name': 'fixture', 'engine': 'codex',
           'account_id': 'account', 'status': 'verified', 'data': value,
           'mode': 'browser', 'browser': 'same_host', 'grantbridge_id': 'remote'}
    bridge.store.create_auth_attempt(row)
    with pytest.raises(BridgeError):
        bridge.authentication._save_remote(row, {**remote, 'status': 'future-success'})
    current = bridge.store.get_auth_attempt('local')
    assert current['status'] == 'failed' and 'verification' not in current['data']
    with pytest.raises(BridgeError) as error:
        bridge.account_login_complete('local', owner_ref='owner')
    assert error.value.code == 'authentication_not_verified'


@pytest.mark.parametrize('remote', [
    {'id': 'remote', 'provider': 'claude', 'status': 'authorized'},
    {'id': 'other', 'provider': 'codex', 'status': 'authorized'},
    {'provider': 'codex', 'status': 'authorized'},
])
def test_auth_attempt_response_is_bound_to_its_identity(remote):
    with pytest.raises(BridgeError):
        attempt(remote, engine='codex', attempt_id='remote')


@pytest.mark.parametrize('value', [
    {'result': {}}, {'jsonrpc': '2.0'},
    {'jsonrpc': '2.0', 'result': {}, 'error': {}},
    {'jsonrpc': '2.0', 'error': 'private-error'},
    {'jsonrpc': '2.0', 'error': {'code': True}},
    {'jsonrpc': '2.0', 'id': True, 'result': {}},
])
def test_jsonrpc_auth_drift_is_not_an_empty_success(value):
    with pytest.raises(BridgeError) as error:
        response_result(value)
    assert error.value.code == 'provider_protocol_error'


def test_future_grantbridge_error_is_safe_and_known_error_stays_actionable():
    for data, code in [([], 'grantbridge_failed'), ({'code': 'private-token'}, 'grantbridge_failed'),
                        ({'code': 'credential_expired'}, 'credential_expired')]:
        with pytest.raises(BridgeError) as error:
            response_result({'jsonrpc': '2.0', 'error': {'code': -32000, 'data': data}})
        assert error.value.code == code and 'private' not in str(error.value)


@pytest.mark.parametrize(('output', 'expected'), [
    ('codex-cli 0.153.0\n', '0.153.0'), ('codex-cli 0.153.0-beta\n', None),
    ('codex-cli 0.153.0+build.2\n', None), ('warning 0.153.0 custom output', None),
    ('codex-cli 0.153.0' + ' ' * 256 + 'custom build', None)])
def test_error_rules_never_treat_future_version_formats_as_stable(tmp_path, monkeypatch, output, expected):
    import agentbridge.error_observer as observer
    monkeypatch.setattr(observer.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=output))
    assert provider_version(Account('fixture', 'codex', home=tmp_path)) == expected


@pytest.mark.parametrize(('key', 'value'), [
    ('timeout', True), ('timeout', '5'), ('timeout', float('nan')),
    ('stop_grace', False), ('max_turns', 1.5), ('max_turns', True),
    ('max_budget_usd', float('nan')), ('max_budget_usd', float('inf')),
    ('context_window', True), ('context_window', 0), ('effort', []), ('model', ''),
    ('timeout', 10**400), ('max_budget_usd', 10**400)])
def test_own_run_contract_rejects_silent_numeric_and_type_coercion(key, value):
    with pytest.raises(BridgeError):
        RunOptions(**{key: value})


def test_provider_model_ids_are_opaque():
    assert RunOptions(model='vendor/new-model:preview').model == 'vendor/new-model:preview'


def test_recover_keeps_unresolved_work_beyond_first_event_page(tmp_path):
    bridge = Bridge(tmp_path / 'store')
    bridge.register(Account('fixture', 'codex', home=tmp_path / 'home'))
    session = bridge.session('fixture', tmp_path)
    bridge.store.admit('run', session['id'], 'fixture', RunOptions(), 'key')
    with bridge.store.connect() as db:
        db.execute("UPDATE runs SET state='running' WHERE id='run'")
    for _ in range(1001):
        bridge.store.emit('run', 'text_delta', {'text': 'x'})
    bridge.store.emit('run', 'tool_call', {'call_id': 'late-call', 'name': 'fixture'})
    assert bridge.recover()['interrupted'] == ['run']
    events = list(bridge.run('run')._observations())
    assert any(e.kind == 'tool_result' and e.data.get('call_id') == 'late-call'
               and e.data.get('outcome') == 'unknown' for e in events)


def test_auth_timestamp_overflow_is_unknown():
    from agentbridge.auth_contract import projection
    assert 'createdAt' not in projection({'createdAt': 10**400})
