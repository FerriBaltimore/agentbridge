"""A local account deletion succeeds only after its proxy stop is durable."""

import socket

import pytest

from agentbridge import Account, Bridge
from agentbridge.errors import BridgeError
from agentbridge.proxy.managed import _names
from agentbridge.rpc import dispatch
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account


def _managed_account(bridge, account_id='managed', name='Managed', port=18311):
    client_name, management_name = _names(account_id)
    seed_authenticated_proxy_account(bridge.store, Account(
        account_id, 'codex', name=name, provider='codex',
        supported_models=('fixture-model',), proxy_base_url=f'http://127.0.0.1:{port}/v1',
        key_env=client_name, management_key_env=management_name))


def test_failed_proxy_stop_keeps_a_durable_tombstone_and_uncertain_result(
        tmp_path, monkeypatch):
    root = tmp_path / 'state'
    bridge = Bridge(root)
    _managed_account(bridge)

    def unavailable(_account_id):
        raise BridgeError('managed_proxy_unavailable', 'Synthetic supervisor failure')

    monkeypatch.setattr(bridge.managed_proxy, 'retire', unavailable)
    with pytest.raises(BridgeError) as failure:
        dispatch(bridge, 'accounts.delete', {'account_ref': 'Managed'})
    assert (failure.value.code, failure.value.phase, failure.value.outcome) == (
        'unknown_outcome', 'execution', 'unknown')
    assert bridge.accounts() == []
    assert dispatch(bridge, 'accounts.status', {'account_ref': 'Managed'})[
        'retirement'] == {'retired': True, 'local_proxy_stopped': False,
                         'managed_proxy': True}
    with bridge.store.connect() as db:
        row = db.execute('SELECT retired_at,proxy_retired_at FROM retired_accounts '
                         'WHERE account_id=?', ('managed',)).fetchone()
    assert row['retired_at'] is not None and row['proxy_retired_at'] is None
    bridge.close()

    reopened = Bridge(root)
    assert reopened.account_status('Managed')['retirement']['local_proxy_stopped'] is False
    calls = []

    def stopped(account_id):
        calls.append(account_id)
        return {'retired': True, 'upstream_credential_removed': False}

    monkeypatch.setattr(reopened.managed_proxy, 'retire', stopped)
    result = reopened.account_delete('Managed')
    assert calls == ['managed']
    assert result == {'account_ref': 'Managed', 'account_id': 'managed', 'removed': True,
                      'upstream_credential_removed': False}
    assert reopened.account_status('Managed')['retirement']['local_proxy_stopped'] is True
    with reopened.store.connect() as db:
        assert db.execute('SELECT proxy_retired_at FROM retired_accounts '
                          'WHERE account_id=?', ('managed',)).fetchone()[0] is not None
    monkeypatch.setattr(reopened.managed_proxy, 'retire', unavailable)
    assert reopened.account_delete('Managed') == result
    reopened.close()


def test_unconfirmed_supervisor_ack_does_not_attest_proxy_stop(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    _managed_account(bridge)
    monkeypatch.setattr(bridge.managed_proxy, 'retire', lambda _: {'retired': False})
    with pytest.raises(BridgeError) as failure:
        bridge.account_delete('Managed')
    assert failure.value.outcome == 'unknown'
    assert bridge.account_status('Managed')['retirement']['local_proxy_stopped'] is False
    bridge.close()


def test_external_proxy_route_retires_without_attesting_or_stopping_it(
        tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        seed_authenticated_proxy_account(bridge.store, Account(
            'external-id', 'codex', name='External', provider='codex',
            supported_models=('fixture-model',),
            proxy_base_url=f'http://127.0.0.1:{port}/v1',
            key_env='FIXTURE_PROXY_KEY', management_key_env='FIXTURE_MANAGEMENT_KEY'))
        monkeypatch.setattr(bridge.managed_proxy, 'retire',
                            lambda _: pytest.fail('External proxy must not be stopped'))
        assert bridge.account_delete('External')['removed'] is True
        assert dispatch(bridge, 'accounts.status', {'account_ref': 'External'})[
            'retirement'] == {'retired': True, 'local_proxy_stopped': False,
                             'managed_proxy': False}
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            pass
    bridge.close()


def test_name_collision_chooses_active_while_typed_id_retries_retired_account(
        tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    _managed_account(bridge, 'original-id', 'Original', 18313)

    def unavailable(_account_id):
        raise BridgeError('managed_proxy_unavailable', 'Synthetic supervisor failure')

    monkeypatch.setattr(bridge.managed_proxy, 'retire', unavailable)
    with pytest.raises(BridgeError) as failure:
        bridge.account_delete('Original')
    assert failure.value.outcome == 'unknown'

    _managed_account(bridge, 'fresh-id', 'original-id', 18314)
    assert bridge.account_service.resolve('original-id').id == 'fresh-id'
    assert dispatch(bridge, 'accounts.status', {'account_ref': 'original-id'})[
        'account_id'] == 'fresh-id'
    assert [item.id for item in bridge.accounts()] == ['fresh-id']
    stopped = []

    def retire(account_id):
        stopped.append(account_id)
        return {'retired': True, 'upstream_credential_removed': False}

    monkeypatch.setattr(bridge.managed_proxy, 'retire', retire)
    assert bridge.account_delete('original-id')['account_ref'] == 'original-id'
    assert stopped == ['fresh-id']
    assert bridge.store.retirement_status('original-id')['local_proxy_stopped'] is False
    result = bridge.account_delete(account_id='original-id')
    assert result['account_ref'] == 'Original'
    assert stopped == ['fresh-id', 'original-id']
    assert bridge.accounts() == []
    assert bridge.store.retirement_status('fresh-id')['local_proxy_stopped'] is True
    bridge.close()


def test_reused_name_status_and_delete_choose_the_active_account(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    _managed_account(bridge, 'old-id', 'Work', 18317)
    stopped = []

    def retire(account_id):
        stopped.append(account_id)
        return {'retired': True, 'upstream_credential_removed': False}

    monkeypatch.setattr(bridge.managed_proxy, 'retire', retire)
    assert bridge.account_delete('Work')['removed'] is True
    _managed_account(bridge, 'new-id', 'Work', 18318)
    status = dispatch(bridge, 'accounts.status', {'account_ref': 'Work'})
    assert status['account_id'] == 'new-id'
    assert status.get('retirement') is None
    assert dispatch(bridge, 'accounts.delete', {'account_ref': 'Work'})['removed'] is True
    assert stopped == ['old-id', 'new-id']
    assert bridge.store.retirement_status('old-id')['local_proxy_stopped'] is True
    assert bridge.store.retirement_status('new-id')['local_proxy_stopped'] is True
    bridge.close()


def test_two_retired_generations_require_typed_status_and_report_exact_delete_id(
        tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    stopped = []

    def retire(account_id):
        stopped.append(account_id)
        return {'retired': True, 'upstream_credential_removed': False}

    monkeypatch.setattr(bridge.managed_proxy, 'retire', retire)
    _managed_account(bridge, 'old-id', 'Work', 18319)
    first = dispatch(bridge, 'accounts.delete', {'account_ref': 'Work'})
    assert first['account_id'] == 'old-id'
    _managed_account(bridge, 'new-id', 'Work', 18320)
    second = dispatch(bridge, 'accounts.delete', {'account_ref': 'Work'})
    assert second['account_id'] == 'new-id'
    assert stopped == ['old-id', 'new-id']
    with pytest.raises(BridgeError) as ambiguous:
        dispatch(bridge, 'accounts.status', {'account_ref': 'Work'})
    assert ambiguous.value.code == 'invalid_request'
    for account_id in ('old-id', 'new-id'):
        status = dispatch(bridge, 'accounts.status', {'account_id': account_id})
        assert status['account_id'] == account_id
        assert status['retirement'] == {'retired': True, 'local_proxy_stopped': True,
                                        'managed_proxy': True}
    assert bridge.account_status(account_id='old-id')['account_id'] == 'old-id'
    bridge.close()


def test_bound_login_preserves_original_account_id_after_name_reuse(
        tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    _managed_account(bridge, 'old-id', 'Work', 18321)
    bridge.store.create_auth_attempt({
        'id': 'old-attempt', 'owner': 'owner', 'account_id': 'old-id',
        'engine': 'codex', 'name': 'Work', 'mode': 'browser',
        'browser': 'same_host', 'status': 'bound', 'data': {},
    })
    completed = dispatch(bridge, 'accounts.login.complete', {
        'attempt_id': 'old-attempt', 'owner_ref': 'owner'})
    assert completed['attempt']['account_id'] == 'old-id'
    monkeypatch.setattr(bridge.managed_proxy, 'retire',
                        lambda _: {'retired': True, 'upstream_credential_removed': False})
    bridge.account_delete('Work')
    _managed_account(bridge, 'new-id', 'Work', 18322)
    historical = dispatch(bridge, 'accounts.login.status', {
        'attempt_id': 'old-attempt', 'owner_ref': 'owner'})
    assert historical['account_id'] == 'old-id'
    bridge.store.update_auth_attempt('old-attempt', 'owner', status='usable')
    usable = dispatch(bridge, 'accounts.login.status', {
        'attempt_id': 'old-attempt', 'owner_ref': 'owner'})
    assert usable['account_id'] == 'old-id'
    assert dispatch(bridge, 'accounts.status', {'account_ref': 'Work'})[
        'account_id'] == 'new-id'
    bridge.close()


def test_active_id_and_name_collision_fails_closed_for_account_ref(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    _managed_account(bridge, 'shared', 'Alpha', 18315)
    _managed_account(bridge, 'second', 'shared', 18316)
    with pytest.raises(BridgeError) as ambiguous:
        bridge.account_delete('shared')
    assert (ambiguous.value.code, ambiguous.value.outcome) == ('invalid_request', 'not_started')
    assert {item.id for item in bridge.accounts()} == {'shared', 'second'}
    assert bridge.store.retirement_status('shared')['retired'] is False
    assert bridge.store.retirement_status('second')['retired'] is False

    stopped = []

    def retire(account_id):
        stopped.append(account_id)
        return {'retired': True, 'upstream_credential_removed': False}

    monkeypatch.setattr(bridge.managed_proxy, 'retire', retire)
    assert dispatch(bridge, 'accounts.delete', {'account_id': 'shared'})['account_ref'] == 'Alpha'
    assert stopped == ['shared']
    assert [item.id for item in bridge.accounts()] == ['second']
    bridge.close()
