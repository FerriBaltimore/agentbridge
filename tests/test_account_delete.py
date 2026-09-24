"""Local account removal keeps history and closes every new execution route."""

import json
from dataclasses import replace

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.auth_proxy_binding import bind_proxy_account
from agentbridge.errors import BridgeError
from agentbridge.commands.account_actions import account_command
from agentbridge.commands.parser import build_parser
from agentbridge.rpc import dispatch
from fixtures.test_proxy_account_fixture import (
    proxy_account, register_verified_proxy_account, seed_authenticated_proxy_account)


def test_removal_preserves_old_records_and_blocks_new_work(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'first', 18301)
    session = bridge.store.add_session('old-session', 'first', str(tmp_path), 'fixture-model')[0]
    run_id = bridge.store.admit('old-turn', session, 'hello', RunOptions(), None)[0]
    bridge.store.finish(run_id, 'completed')

    result = bridge.account_delete('first')
    assert result == {'account_ref': 'first', 'account_id': 'first', 'removed': True,
                      'upstream_credential_removed': False}
    assert bridge.accounts() == []
    assert bridge.get_session(session)['account_id'] == 'first'
    assert bridge.turn(run_id)['account_ref'] == 'first'
    assert bridge.account_status('first')['authentication']['status'] == 'retired'
    assert bridge.account_usage('first')['reason'] == 'account_removed'
    with pytest.raises(BridgeError) as error:
        bridge.store.add_session('new-session', 'first', str(tmp_path), 'fixture-model')
    assert error.value.code == 'account_retired'
    with pytest.raises(BridgeError) as error:
        bridge.store.admit('new-turn', session, 'again', RunOptions(), None)
    assert error.value.code == 'account_retired'
    assert bridge.account_delete('first') == result
    assert bridge.store.proxy_binding('first') is None
    with pytest.raises(BridgeError) as error:
        bridge.account_login_complete('fixture-login-first', owner_ref='fixture-owner')
    assert error.value.code == 'authentication_required'
    assert bridge.store.get_auth_attempt('fixture-login-first', 'fixture-owner')['status'] == 'bound'
    with bridge.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM auth_proxy_routes').fetchone()[0] == 0


def test_removal_waits_for_active_turn_and_pending_login(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'first', 18302)
    bridge.store.add_session('session', 'first', str(tmp_path), 'fixture-model')
    bridge.store.admit('active', 'session', 'hello', RunOptions(), None)
    with pytest.raises(BridgeError) as error:
        bridge.account_delete('first')
    assert error.value.code == 'busy'
    bridge.store.finish('active', 'completed')

    attempt = {'id': 'pending-login', 'owner': 'fixture-owner', 'account_id': 'first',
               'engine': 'fixture', 'name': 'first', 'mode': 'browser',
               'browser': 'same_host', 'status': 'awaiting_user', 'data': {}}
    bridge.store.create_auth_attempt(attempt, proxy_route={
        'proxy_base_url': 'http://127.0.0.1:18302/v1',
        'key_env': 'FIXTURE_PROXY_KEY',
        'management_key_env': 'FIXTURE_MANAGEMENT_KEY'})
    with pytest.raises(BridgeError) as error:
        bridge.account_delete('first')
    assert error.value.code == 'authentication_in_progress'
    bridge.store.update_auth_attempt('pending-login', 'fixture-owner', status='cancelled')
    assert bridge.account_delete('first')['removed']


def test_retired_name_and_endpoint_can_be_bound_to_new_account(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'old', 18303)
    bridge.account_delete('old')
    assert bridge.account_service.list() == []
    route = {'proxy_base_url': 'http://127.0.0.1:18303/v1',
             'key_env': 'FIXTURE_PROXY_KEY', 'management_key_env': 'FIXTURE_MANAGEMENT_KEY'}
    attempt = {'id': 'new-login', 'owner': 'new-owner', 'account_id': 'new',
               'engine': 'fixture', 'name': 'old', 'mode': 'browser',
               'browser': 'same_host', 'status': 'verified', 'data': {},
               'grantbridge_id': 'fixture-grantbridge'}
    bridge.store.create_auth_attempt(attempt, proxy_route=route)
    observed = {'provider': 'fixture', 'status': 'active', 'disabled': False,
                'unavailable': False, 'binding_fingerprint': 'c' * 64,
                'identity_fingerprint': 'd' * 64, 'models': [{'id': 'fixture-model'}]}
    account, _ = bind_proxy_account(bridge.store, attempt, route, observed)
    assert account.name == 'old' and account.id == 'new'
    assert [item.id for item in bridge.accounts()] == ['new']


def test_incompatible_saved_account_does_not_break_listing(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    with bridge.store.connect() as db:
        db.execute('INSERT INTO accounts(id,config) VALUES (?,?)',
                   ('incompatible', json.dumps({'id': 'incompatible', 'engine': 'unknown'})))
    assert bridge.accounts() == []
    with pytest.raises(BridgeError) as error:
        bridge.account('incompatible')
    assert error.value.code == 'account_unavailable'


def test_delete_is_declared_and_uses_the_same_sdk_method_over_rpc(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'first', 18304)
    capability = bridge.capabilities()['operations']['accounts.delete']
    assert capability['support'] == 'adapter'
    assert 'upstream_credential_remains' in capability['limitations']
    result = dispatch(bridge, 'accounts.delete', {'account_ref': 'first'})
    assert result == {'account_ref': 'first', 'account_id': 'first', 'removed': True,
                      'upstream_credential_removed': False}
    assert dispatch(bridge, 'accounts.list', {}) == []


def test_retired_name_still_resolves_for_historical_status_and_usage(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    account = replace(proxy_account('private-id', 18305), name='Personal')
    seed_authenticated_proxy_account(bridge.store, account)
    assert bridge.account_delete('Personal')['removed']
    assert bridge.account_status(account_ref='Personal')['authentication']['status'] == 'retired'
    assert bridge.account_usage(account_ref='Personal')['reason'] == 'account_removed'
    assert bridge.account_usage_history('Personal') == []


def test_cli_delete_has_an_exact_account_id_retry_path():
    class BridgeStub:
        def account_delete(self, account_ref=None, *, account_id=None):
            return {'account_ref': account_ref, 'account_id': account_id}

    parser, _ = build_parser()
    by_id = parser.parse_args(['accounts', 'delete', '--account-id', 'retired-id'])
    assert account_command(BridgeStub(), by_id) == {
        'account_ref': None, 'account_id': 'retired-id'}
    by_name = parser.parse_args(['accounts', 'delete', 'Personal'])
    assert account_command(BridgeStub(), by_name) == {
        'account_ref': 'Personal', 'account_id': None}
    for arguments in (['accounts', 'delete'],
                      ['accounts', 'delete', 'Personal', '--account-id', 'retired-id']):
        with pytest.raises(BridgeError) as error:
            account_command(BridgeStub(), parser.parse_args(arguments))
        assert error.value.code == 'invalid_request'
