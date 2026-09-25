"""GrantBridge proxy OAuth creates only a verified, routed account."""

import json
import secrets
import shutil
import threading
from pathlib import Path

import pytest

from agentbridge import Account, Bridge, GrantBridgeClient
from agentbridge.errors import BridgeError
from test_proxy_management import EMPTY_CONFIG, local_management


class FakeProxyGrantBridge:
    def __init__(self, responses, provider='codex'):
        self.responses = responses
        self.provider = provider
        self.closed = False

    def proxy_start(self, provider, base_url, management_key_env):
        assert provider == self.provider and base_url.endswith('/v1')
        assert management_key_env == 'LAB_MANAGEMENT_KEY'
        self.responses['/v0/management/auth-files'] = (200, {
            'files': [{
                'name': 'one.json', 'source': 'file', 'runtime_only': False,
                'provider': provider, 'status': 'active', 'disabled': False,
                'unavailable': False, 'auth_index': 'fixture-index',
                'account_type': 'oauth', 'email': 'person@example.test',
                'id_token': {'chatgpt_account_id': 'fixture-account'},
                'cooldowns': [],
            }]}, {})
        return {'id': 'oauth-state', 'provider': provider, 'status': 'awaiting_user',
                'authorizationUrl': 'https://auth.example.test/authorize'}

    def proxy_status(self, state, provider, base_url, management_key_env):
        assert (state, provider, management_key_env) == (
            'oauth-state', self.provider, 'LAB_MANAGEMENT_KEY')
        return {'id': state, 'provider': provider, 'status': 'authorized'}

    def proxy_cancel(self, state, provider, base_url, management_key_env):
        return {'id': state, 'provider': provider, 'status': 'cancelled'}

    def proxy_callback(self, state, provider, base_url, management_key_env, redirect_url):
        assert (state, provider, management_key_env) == (
            'oauth-state', self.provider, 'LAB_MANAGEMENT_KEY')
        assert redirect_url.startswith('http://localhost:1455/auth/callback?')
        return {'id': state, 'provider': provider, 'status': 'awaiting_user'}

    def close(self):
        self.closed = True

    def configuration(self):
        return {'adapter': 'fixture', 'data_dir': None, 'node': 'fixture'}


def use_fake_grantbridge(monkeypatch, fake):
    monkeypatch.setattr('agentbridge.authentication.GrantBridgeClient',
                        lambda *args, **kwargs: fake)


def proxy_responses():
    return {
        '/v0/management/auth-files': (200, {'files': []}, {}),
        '/v0/management/config': (200, EMPTY_CONFIG, {}),
        '/v0/management/auth-files/models?name=one.json': (
            200, {'models': [{'id': 'fixture/model'}]}, {}),
    }


def test_one_login_creates_only_a_verified_proxy_account(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            fake = FakeProxyGrantBridge(responses)
            use_fake_grantbridge(monkeypatch, fake)
            options = {'provider': 'codex', 'name': '  Primary  ',
                       'proxy_base_url': f'http://127.0.0.1:{port}/v1',
                       'key_env': 'LAB_PROXY_KEY',
                       'management_key_env': 'LAB_MANAGEMENT_KEY'}
            started = bridge.account_login_start(**options, request_key='login-1')
            replay = bridge.account_login_start(**options, request_key='login-1')
            assert replay['attempt_id'] == started['attempt_id']
            assert started['status'] == 'awaiting_user'
            current = bridge.account_login_status(
                started['attempt_id'], owner_ref=started['owner_ref'])
            assert current['status'] == 'authorized'
            assert current['authorization_url'] == started['authorization_url']
            checked = bridge.account_login_check(
                started['attempt_id'], owner_ref=started['owner_ref'])
            assert checked['status'] == 'verified'
            result = bridge.account_login_complete(
                started['attempt_id'], owner_ref=started['owner_ref'])
            assert result['attempt']['status'] == 'bound'
            account = bridge.resolve_account('Primary')
            assert account.name == '  Primary  '
            assert account.engine == 'codex'
            assert account.provider == 'codex'
            assert account.home is None
            assert account.supported_models == ('fixture/model',)
            assert account.proxy_base_url == options['proxy_base_url']
            assert bridge.store.proxy_binding(account.id)
            assert 'fixture-account' not in repr(result)
            assert 'fixture-index' not in repr(result)


def test_owned_remote_callback_is_transient_and_matches_pending_state(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses))
            started = bridge.account_login_start(
                provider='codex', name='Remote',
                proxy_base_url=f'http://127.0.0.1:{port}/v1',
                key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY')
            url = 'http://localhost:1455/auth/callback?code=private-code&state=oauth-state'
            with pytest.raises(BridgeError) as wrong_owner:
                bridge.account_login_callback(started['attempt_id'],
                                              owner_ref='another-owner', redirect_url=url)
            assert wrong_owner.value.code != 'invalid_request'
            with pytest.raises(BridgeError) as wrong_state:
                bridge.account_login_callback(started['attempt_id'],
                    owner_ref=started['owner_ref'],
                    redirect_url=url.replace('oauth-state', 'another-state'))
            assert wrong_state.value.code == 'invalid_request'
            delivered = bridge.account_login_callback(
                started['attempt_id'], owner_ref=started['owner_ref'], redirect_url=url)
            assert delivered['status'] == 'awaiting_user'
            saved = bridge.store.get_auth_attempt(started['attempt_id'], started['owner_ref'])
            assert 'private-code' not in repr(saved)


def test_expected_email_rejects_a_different_proxy_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses))
            started = bridge.account_login_start(
                provider='codex', name='Work', email='expected@example.test',
                proxy_base_url=f'http://127.0.0.1:{port}/v1',
                key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY')
            with pytest.raises(BridgeError) as error:
                bridge.account_login_check(
                    started['attempt_id'], owner_ref=started['owner_ref'])
            assert error.value.code == 'identity_changed'
            assert bridge.accounts() == []


def test_login_cannot_convert_a_direct_account_to_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    with local_management(proxy_responses()) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(proxy_responses()))
            original = Account('old-account', 'codex', home=tmp_path / 'old',
                               name='Primary', email='person@example.test')
            with bridge.store.connect() as db:
                db.execute('INSERT INTO accounts(id,config) VALUES (?,?)',
                           (original.id, json.dumps(original.to_dict())))
            with pytest.raises(BridgeError) as error:
                bridge.account_login_start(
                    provider='codex', name='Primary',
                    proxy_base_url=f'http://127.0.0.1:{port}/v1',
                    key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY')
            assert error.value.code == 'account_migration_required'
            assert bridge.resolve_account('Primary') == original


def test_cancel_does_not_claim_success_after_proxy_authorized(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    responses = proxy_responses()

    class CompletedDuringCancel(FakeProxyGrantBridge):
        def proxy_cancel(self, state, provider, base_url, management_key_env):
            return {'id': state, 'provider': provider, 'status': 'authorized'}

    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            fake = CompletedDuringCancel(responses)
            use_fake_grantbridge(monkeypatch, fake)
            started = bridge.account_login_start(
                provider='codex', name='Primary',
                proxy_base_url=f'http://127.0.0.1:{port}/v1',
                key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY')
            with pytest.raises(BridgeError) as error:
                bridge.account_login_cancel(
                    started['attempt_id'], owner_ref=started['owner_ref'])
            assert error.value.code == 'already_finished'
            assert bridge.account_login_status(
                started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'authorized'
            assert bridge.account_login_check(
                started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'verified'


@pytest.mark.parametrize('operation', ['status', 'cancel'])
def test_unknown_proxy_session_requires_explicit_local_abandonment(tmp_path, monkeypatch, operation):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    responses = proxy_responses()

    class UnknownSession(FakeProxyGrantBridge):
        def proxy_status(self, *args):
            raise BridgeError('authentication_outcome_unknown', 'Fixture state expired.')

        def proxy_cancel(self, *args):
            raise BridgeError('authentication_outcome_unknown', 'Fixture state expired.')

    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            fake = UnknownSession(responses)
            use_fake_grantbridge(monkeypatch, fake)
            started = bridge.account_login_start(
                provider='codex', name='Primary',
                proxy_base_url=f'http://127.0.0.1:{port}/v1',
                key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY')
            if operation == 'status':
                result = bridge.account_login_status(
                    started['attempt_id'], owner_ref=started['owner_ref'])
                assert result['status'] == 'interrupted'
            else:
                with pytest.raises(BridgeError) as error:
                    bridge.account_login_cancel(
                        started['attempt_id'], owner_ref=started['owner_ref'])
                assert error.value.code == 'authentication_outcome_unknown'
            interrupted = bridge.account_login_status(
                started['attempt_id'], owner_ref=started['owner_ref'])
            assert interrupted['status'] == 'interrupted'
            assert interrupted['error'] == {'code': 'authentication_outcome_unknown'}
            with pytest.raises(BridgeError) as busy:
                bridge.account_login_start(
                    provider='codex', name='Primary',
                    proxy_base_url=f'http://127.0.0.1:{port}/v1',
                    key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY',
                    request_key='different')
            assert busy.value.code == 'busy'
            assert bridge.account_login_cancel(
                started['attempt_id'], owner_ref=started['owner_ref'])['status'] == 'abandoned'
            assert bridge.accounts() == []
            with pytest.raises(BridgeError) as retired:
                bridge.account_login_start(
                    provider='codex', name='Primary',
                    proxy_base_url=f'http://127.0.0.1:{port}/v1',
                    key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY',
                    request_key='after-abandonment')
            assert retired.value.code == 'proxy_endpoint_retired'
            fresh_responses = proxy_responses()
            with local_management(fresh_responses) as (fresh_port, _):
                use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(fresh_responses))
                fresh = bridge.account_login_start(
                    provider='codex', name='Primary',
                    proxy_base_url=f'http://127.0.0.1:{fresh_port}/v1',
                    key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY',
                    request_key='fresh-endpoint')
                assert fresh['status'] == 'awaiting_user'


def test_public_login_rejects_private_client_injection(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        with pytest.raises(TypeError):
            bridge.account_login_start(
                provider='codex', name='Fixture',
                proxy_base_url='http://127.0.0.1:8317/v1',
                key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY',
                client=object())


def test_bound_endpoint_rejects_new_name_before_oauth(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            fake = FakeProxyGrantBridge(responses)
            use_fake_grantbridge(monkeypatch, fake)
            route = {'proxy_base_url': f'http://127.0.0.1:{port}/v1',
                     'key_env': 'LAB_PROXY_KEY',
                     'management_key_env': 'LAB_MANAGEMENT_KEY'}
            started = bridge.account_login_start(provider='codex', name='First', **route)
            bridge.account_login_check(started['attempt_id'], owner_ref=started['owner_ref'])
            bridge.account_login_complete(started['attempt_id'], owner_ref=started['owner_ref'])
            responses['/v0/management/auth-files'] = (200, {'files': []}, {})

            def unexpected_start(*args):
                raise AssertionError('OAuth must not start on another account endpoint.')

            fake.proxy_start = unexpected_start
            with pytest.raises(BridgeError) as error:
                bridge.account_login_start(provider='codex', name='Second', **route)
            assert error.value.code == 'proxy_endpoint_shared'


def test_proxy_login_rejects_shared_client_and_management_key_name(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        with pytest.raises(BridgeError) as error:
            bridge.account_login_start(
                provider='codex', name='Fixture',
                proxy_base_url='http://127.0.0.1:8317/v1',
                key_env='SHARED_PROXY_KEY', management_key_env='SHARED_PROXY_KEY')
        assert error.value.code == 'invalid_environment'


def test_same_provider_oauth_attempts_are_serialized_for_callback_port(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))
    first_responses = proxy_responses()
    second_responses = proxy_responses()
    with local_management(first_responses) as (first_port, _):
        with local_management(second_responses) as (second_port, _):
            with Bridge(tmp_path / 'state') as bridge:
                use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(first_responses))
                first = bridge.account_login_start(
                    provider='codex', name='First',
                    proxy_base_url=f'http://127.0.0.1:{first_port}/v1',
                    key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY')
                assert first['status'] == 'awaiting_user'
                with pytest.raises(BridgeError) as error:
                    bridge.account_login_start(
                        provider='codex', name='Second',
                        proxy_base_url=f'http://127.0.0.1:{second_port}/v1',
                        key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY')
                assert error.value.code == 'authentication_in_progress'


def test_cancel_during_proxy_start_never_claims_remote_cancellation(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    responses = proxy_responses()

    class BlockingStart(FakeProxyGrantBridge):
        def __init__(self, proxy_responses):
            super().__init__(proxy_responses)
            self.entered = threading.Event()
            self.release = threading.Event()

        def proxy_start(self, provider, base_url, management_key_env):
            self.entered.set()
            assert self.release.wait(5)
            return super().proxy_start(provider, base_url, management_key_env)

    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            fake = BlockingStart(responses)
            use_fake_grantbridge(monkeypatch, fake)
            start_results = []

            def start_login():
                try:
                    start_results.append(bridge.account_login_start(
                        provider='codex', name='Fixture',
                        proxy_base_url=f'http://127.0.0.1:{port}/v1',
                        key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY',
                        owner_ref='fixture-owner'))
                except BridgeError as error:
                    start_results.append(error.code)

            thread = threading.Thread(target=start_login, daemon=True)
            thread.start()
            assert fake.entered.wait(5)
            with bridge.store.connect() as db:
                row = db.execute("SELECT id FROM auth_attempts WHERE owner='fixture-owner'").fetchone()
            assert row is not None
            try:
                try:
                    cancelled = bridge.account_login_cancel(row['id'], owner_ref='fixture-owner')
                    assert cancelled['status'] in {'interrupted', 'abandoned'}
                except BridgeError as error:
                    assert error.code == 'authentication_outcome_unknown'
            finally:
                fake.release.set()
                thread.join(timeout=5)
            assert not thread.is_alive()
            assert start_results
            current = bridge.account_login_status(row['id'], owner_ref='fixture-owner')
            assert current['status'] in {'interrupted', 'abandoned'}
            assert current['status'] != 'cancelled'


def test_grantbridge_proxy_adapter_starts_and_polls_a_local_fixture(monkeypatch):
    monkeypatch.setattr('agentbridge.grantbridge.ensure_callback_port_available',
                        lambda provider: None)
    if not shutil.which('node'):
        pytest.skip('Node.js is required for the local GrantBridge adapter.')
    checkout = Path(__file__).resolve().parents[2] / 'grantbridge'
    if not (checkout / 'scripts' / 'agentbridge-adapter.mjs').is_file():
        pytest.skip('The local GrantBridge checkout is unavailable.')
    secret = secrets.token_hex(16)
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secret)
    responses = {
        '/v0/management/codex-auth-url?is_webui=true': (
            200, {'status': 'ok', 'url': 'https://auth.example.test/authorize',
                  'state': 'fixture-oauth-state'}, {}),
        '/v0/management/get-auth-status?state=fixture-oauth-state': (
            200, {'status': 'ok'}, {}),
    }
    with local_management(responses) as (port, seen):
        base_url = f'http://127.0.0.1:{port}/v1'
        with GrantBridgeClient(checkout) as grantbridge:
            started = grantbridge.proxy_start('codex', base_url, 'LAB_MANAGEMENT_KEY')
            current = grantbridge.proxy_status(
                started['id'], 'codex', base_url, 'LAB_MANAGEMENT_KEY')
    assert started == {'id': 'fixture-oauth-state', 'provider': 'codex',
                       'status': 'awaiting_user',
                       'authorizationUrl': 'https://auth.example.test/authorize'}
    assert current == {'id': started['id'], 'provider': 'codex', 'status': 'authorized'}
    assert all(header == f'Bearer {secret}' for _, header in seen)
    assert secret not in repr(started) + repr(current)
