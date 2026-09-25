"""Managed login terminal cleanup and identity recovery regressions."""

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from test_managed_account_login import FixtureManagedProxy
from test_proxy_authentication import FakeProxyGrantBridge, proxy_responses, use_fake_grantbridge
from test_proxy_management import local_management


@pytest.mark.parametrize(('source', 'terminal'), [
    ('start', 'failed'), ('status', 'failed'), ('status', 'expired'),
    ('cancel', 'failed'), ('cancel', 'cancelled'),
])
def test_terminal_login_retires_only_after_status_is_durable(tmp_path, monkeypatch,
                                                             source, terminal):
    responses = proxy_responses()

    class TerminalGrantBridge(FakeProxyGrantBridge):
        def proxy_start(self, *args):
            started = super().proxy_start(*args)
            return {**started, 'status': terminal} if source == 'start' else started

        def proxy_status(self, state, provider, base_url, management_key_env):
            return {'id': state, 'provider': provider, 'status': terminal}

        def proxy_cancel(self, state, provider, base_url, management_key_env):
            return {'id': state, 'provider': provider, 'status': terminal}

    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            use_fake_grantbridge(monkeypatch, TerminalGrantBridge(responses, provider='claude'))

            def confirm_saved(account_id):
                saved = bridge.store.latest_auth_attempt(account_id)
                assert saved['status'] == terminal

            managed.on_retire = confirm_saved
            started = bridge.account_login_start(
                provider='claude', name='Fixture', request_key='terminal-login')
            if source == 'status':
                observed = bridge.account_login_status(
                    started['attempt_id'], owner_ref=started['owner_ref'])
            elif source == 'cancel':
                observed = bridge.account_login_cancel(
                    started['attempt_id'], owner_ref=started['owner_ref'])
            else:
                observed = started
            assert observed['status'] == terminal
            assert len(managed.retires) == 1
            assert bridge.accounts() == []
            assert bridge.account_login_status(
                started['attempt_id'], owner_ref=started['owner_ref'])['status'] == terminal
            assert len(managed.retires) == 2
            assert managed.retires[0] == managed.retires[1]


def test_failed_proxy_stop_can_be_retried_from_saved_status(tmp_path, monkeypatch):
    responses = proxy_responses()

    class FailedGrantBridge(FakeProxyGrantBridge):
        def proxy_status(self, state, provider, base_url, management_key_env):
            return {'id': state, 'provider': provider, 'status': 'failed'}

    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            use_fake_grantbridge(monkeypatch, FailedGrantBridge(responses))
            started = bridge.account_login_start(provider='codex', name='Fixture')

            def unverified(_account_id):
                raise BridgeError('managed_proxy_stop_unverified', 'Synthetic stop failure.')

            managed.on_retire = unverified
            with pytest.raises(BridgeError) as failure:
                bridge.account_login_status(
                    started['attempt_id'], owner_ref=started['owner_ref'])
            assert failure.value.code == 'managed_proxy_stop_unverified'
            assert failure.value.safe_data()['category'] == 'execution'
            assert failure.value.safe_data()['outcome'] == 'unknown'
            assert bridge.store.get_auth_attempt(
                started['attempt_id'], started['owner_ref'])['status'] == 'failed'
            managed.on_retire = None
            assert bridge.account_login_status(
                started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'failed'
            assert len(managed.retires) == 2


@pytest.mark.parametrize(('failure', 'original_code'), [
    ('create', 'busy'), ('precheck', 'proxy_not_empty'),
    ('known_start', 'oauth_callback_port_busy'),
])
def test_known_start_failure_reports_unverified_proxy_stop(tmp_path, monkeypatch,
                                                           failure, original_code):
    responses = proxy_responses()
    if failure == 'precheck':
        responses['/v0/management/auth-files'] = (200, {'files': [{'name': 'one.json'}]}, {})

    class BusyGrantBridge(FakeProxyGrantBridge):
        def proxy_start(self, *args):
            raise BridgeError('oauth_callback_port_busy', 'Synthetic callback conflict.')

    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            client = (BusyGrantBridge(responses) if failure == 'known_start'
                      else FakeProxyGrantBridge(responses))
            use_fake_grantbridge(monkeypatch, client)
            if failure == 'create':
                def reject_attempt(*args, **kwargs):
                    raise BridgeError('busy', 'Synthetic store conflict.')

                monkeypatch.setattr(bridge.store, 'create_auth_attempt', reject_attempt)

            def unverified(account_id):
                if failure != 'create':
                    assert bridge.store.latest_auth_attempt(account_id)['status'] == 'failed'
                raise BridgeError('managed_proxy_unavailable', 'Synthetic stop failure.')

            managed.on_retire = unverified
            with pytest.raises(BridgeError) as stop:
                bridge.account_login_start(provider='codex', name='Fixture')
            assert stop.value.code == 'managed_proxy_stop_unverified'
            assert stop.value.safe_data()['category'] == 'execution'
            assert stop.value.safe_data()['outcome'] == 'unknown'
            assert len(managed.retires) == 1
            if failure == 'create':
                assert stop.value.details['prior_error_code'] == original_code
                assert bridge.accounts() == []
                assert bridge.store.latest_auth_attempt(managed.retires[0]) is None
            else:
                saved = bridge.store.latest_auth_attempt(managed.retires[0])
                assert saved['data']['error'] == {'code': original_code}
                assert stop.value.details['attempt_id'] == saved['id']
                managed.on_retire = None
                assert bridge.account_login_status(
                    saved['id'], owner_ref=saved['owner'])['status'] == 'failed'
                assert len(managed.retires) == 2


def test_confirmed_cancel_retries_unverified_stop_without_new_proxy_call(tmp_path, monkeypatch):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            client = FakeProxyGrantBridge(responses)
            use_fake_grantbridge(monkeypatch, client)
            started = bridge.account_login_start(provider='codex', name='Fixture')

            def unverified(_account_id):
                raise BridgeError('managed_proxy_unavailable', 'Synthetic stop failure.')

            managed.on_retire = unverified
            with pytest.raises(BridgeError) as stop:
                bridge.account_login_cancel(
                    started['attempt_id'], owner_ref=started['owner_ref'])
            assert stop.value.code == 'managed_proxy_stop_unverified'
            assert bridge.store.get_auth_attempt(
                started['attempt_id'], started['owner_ref'])['status'] == 'cancelled'
            managed.on_retire = None
            assert bridge.account_login_status(
                started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'cancelled'
            assert len(managed.retires) == 2


def test_unknown_oauth_outcome_keeps_managed_proxy_running(tmp_path, monkeypatch):
    responses = proxy_responses()

    class UnknownGrantBridge(FakeProxyGrantBridge):
        def proxy_status(self, *args):
            raise BridgeError('authentication_outcome_unknown', 'Synthetic lost response.')

    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            use_fake_grantbridge(monkeypatch, UnknownGrantBridge(responses))
            started = bridge.account_login_start(provider='codex', name='Fixture')
            assert bridge.account_login_status(
                started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'interrupted'
            assert bridge.account_login_cancel(
                started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'abandoned'
            assert managed.retires == []


def test_failed_reauthentication_preserves_bound_account_proxy(tmp_path, monkeypatch):
    responses = proxy_responses()

    class FailedReauthentication(FakeProxyGrantBridge):
        def proxy_status(self, state, provider, base_url, management_key_env):
            return {'id': state, 'provider': provider, 'status': 'failed'}

    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses))
            first = bridge.account_login_start(provider='codex', name='Fixture')
            assert bridge.account_login_check(
                first['attempt_id'], owner_ref=first['owner_ref'])['status'] == 'verified'
            account = bridge.account_login_complete(
                first['attempt_id'], owner_ref=first['owner_ref'])['account']

            use_fake_grantbridge(monkeypatch, FailedReauthentication(responses))
            second = bridge.account_login_start(provider='codex', name='Fixture')
            assert bridge.account_login_status(
                second['attempt_id'], owner_ref=second['owner_ref'])['status'] == 'failed'
            assert bridge.account_login_status(
                second['attempt_id'], owner_ref=second['owner_ref'])['status'] == 'failed'
            assert bridge.resolve_account('Fixture').id == account['id']
            assert managed.retires == []


def test_adapter_loss_after_provision_persists_failure_and_stops_proxy(tmp_path, monkeypatch):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            constructions = []

            def make_adapter(*args, **kwargs):
                constructions.append(None)
                if len(constructions) == 1:
                    return FakeProxyGrantBridge(responses)
                raise BridgeError('grantbridge_unavailable', 'Synthetic adapter loss.')

            monkeypatch.setattr('agentbridge.authentication.GrantBridgeClient', make_adapter)
            with pytest.raises(BridgeError) as failure:
                bridge.account_login_start(provider='codex', name='Fixture')
            assert failure.value.code == 'grantbridge_unavailable'
            assert len(constructions) == 2
            assert len(managed.retires) == 1
            saved = bridge.store.latest_auth_attempt(managed.retires[0])
            assert saved['status'] == 'failed'
            assert saved['data']['error'] == {'code': 'grantbridge_unavailable'}


def test_mismatched_new_claude_email_fails_and_allows_fresh_login(tmp_path, monkeypatch):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses, provider='claude'))

            def confirm_failed(account_id):
                saved = bridge.store.latest_auth_attempt(account_id)
                assert saved['status'] == 'failed'
                assert saved['data']['error'] == {'code': 'identity_changed'}

            managed.on_retire = confirm_failed
            first = bridge.account_login_start(
                provider='claude', name='Work', email='expected@example.test',
                request_key='first')
            with pytest.raises(BridgeError) as mismatch:
                bridge.account_login_check(
                    first['attempt_id'], owner_ref=first['owner_ref'])
            assert mismatch.value.code == 'identity_changed'
            assert len(managed.retires) == 1
            assert bridge.accounts() == []
            with pytest.raises(BridgeError) as rejected:
                bridge.account_login_complete(
                    first['attempt_id'], owner_ref=first['owner_ref'])
            assert rejected.value.code == 'authentication_not_verified'

            responses['/v0/management/auth-files'] = (200, {'files': []}, {})
            managed.on_retire = None
            second = bridge.account_login_start(
                provider='claude', name='Work', email='person@example.test',
                request_key='second')
            assert second['status'] == 'awaiting_user'
            assert bridge.account_login_check(
                second['attempt_id'], owner_ref=second['owner_ref'])['status'] == 'verified'
            account = bridge.account_login_complete(
                second['attempt_id'], owner_ref=second['owner_ref'])['account']
            assert account['email'] == 'person@example.test'
            assert account['id'] != managed.retires[0]


def test_mismatched_reauthentication_keeps_bound_proxy_and_attempt(tmp_path, monkeypatch):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses))
            first = bridge.account_login_start(provider='codex', name='Work')
            assert bridge.account_login_check(
                first['attempt_id'], owner_ref=first['owner_ref'])['status'] == 'verified'
            account = bridge.account_login_complete(
                first['attempt_id'], owner_ref=first['owner_ref'])['account']

            second = bridge.account_login_start(
                provider='codex', name='Work', email='wrong@example.test')
            with pytest.raises(BridgeError) as mismatch:
                bridge.account_login_check(
                    second['attempt_id'], owner_ref=second['owner_ref'])
            assert mismatch.value.code == 'identity_changed'
            assert bridge.store.get_auth_attempt(
                second['attempt_id'], second['owner_ref'])['status'] == 'authorized'
            assert bridge.resolve_account('Work').id == account['id']
            assert managed.retires == []


def test_claude_expected_email_accepts_case_only_difference(tmp_path, monkeypatch):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses, provider='claude'))
            started = bridge.account_login_start(
                provider='claude', name='Work', email='PERSON@EXAMPLE.TEST')
            assert bridge.account_login_check(
                started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'verified'
            account = bridge.account_login_complete(
                started['attempt_id'], owner_ref=started['owner_ref'])['account']
            assert account['email'] == 'person@example.test'
            assert managed.retires == []


@pytest.mark.parametrize('email', [7, '', ' person@example.test', 'person@example.test\n'])
def test_invalid_expected_email_is_rejected_before_provision(tmp_path, email):
    with Bridge(tmp_path / 'state') as bridge:
        with pytest.raises(BridgeError) as error:
            bridge.account_login_start(provider='claude', name='Work', email=email)
        assert error.value.code == 'invalid_request'
        assert bridge.account_login_attempts() == []


def test_missing_observed_email_does_not_fail_or_retire_attempt(tmp_path, monkeypatch):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.authentication.managed_proxy = managed
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses))
            started = bridge.account_login_start(
                provider='codex', name='Work', email='person@example.test')
            entry = responses['/v0/management/auth-files'][1]['files'][0]
            entry['email'] = None
            with pytest.raises(BridgeError) as unavailable:
                bridge.account_login_check(
                    started['attempt_id'], owner_ref=started['owner_ref'])
            assert unavailable.value.code == 'identity_changed'
            assert bridge.store.get_auth_attempt(
                started['attempt_id'], started['owner_ref'])['status'] == 'authorized'
            assert managed.retires == []
            entry['email'] = 'person@example.test'
            assert bridge.account_login_check(
                started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'verified'
