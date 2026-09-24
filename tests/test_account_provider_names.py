"""Account names are unique within a provider, including login races."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import secrets
from threading import Barrier

import pytest

from agentbridge import Account, Bridge
from agentbridge.auth_proxy_binding import bind_proxy_account
from agentbridge.errors import BridgeError
from agentbridge.rpc import dispatch
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from test_proxy_authentication import (
    FakeProxyGrantBridge,
    proxy_responses,
    use_fake_grantbridge,
)
from test_proxy_management import local_management


def _account(account_id, provider, name, port):
    return Account(
        account_id, 'codex', name=name, provider=provider,
        supported_models=('fixture/model',),
        proxy_base_url=f'http://127.0.0.1:{port}/v1',
        key_env=f'LAB_{account_id.upper()}_CLIENT_KEY',
        management_key_env=f'LAB_{account_id.upper()}_MANAGEMENT_KEY',
    )


def _attempt(account_id, provider, name):
    return {
        'id': f'attempt-{account_id}', 'owner': f'owner-{account_id}',
        'account_id': account_id, 'engine': provider, 'name': name,
        'mode': 'browser', 'browser': 'same_host', 'status': 'verified',
        'data': {},
    }


def _route(port):
    return {
        'proxy_base_url': f'http://127.0.0.1:{port}/v1',
        'key_env': 'LAB_PROXY_KEY',
        'management_key_env': 'LAB_MANAGEMENT_KEY',
    }


def _observation(provider, account_id):
    return {
        'provider': provider, 'status': 'active', 'disabled': False,
        'unavailable': False, 'email': f'{account_id}@example.test',
        'models': [{'id': 'fixture/model'}],
        'binding_fingerprint': sha256(f'binding:{account_id}'.encode()).hexdigest(),
        'identity_fingerprint': sha256(f'identity:{account_id}'.encode()).hexdigest(),
    }


def test_cross_provider_name_can_complete_login_and_keeps_original_spelling(
        tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            seed_authenticated_proxy_account(
                bridge.store, _account('old', 'codex', 'Shared', 18331))
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses, 'claude'))
            started = bridge.account_login_start(
                provider='claude', name='  shared  ', **_route(port))
            bridge.account_login_check(started['attempt_id'], owner_ref=started['owner_ref'])
            completed = bridge.account_login_complete(
                started['attempt_id'], owner_ref=started['owner_ref'])
            assert completed['account']['name'] == '  shared  '
            assert completed['attempt']['account_ref'].startswith('id:')
            assert {(account.provider, account.name) for account in bridge.accounts()} == {
                ('codex', 'Shared'), ('claude', '  shared  ')}
            references = {account.provider: bridge.account_reference(account.id)
                          for account in bridge.accounts()}
            assert all(reference.startswith('id:') for reference in references.values())
            for provider, reference in references.items():
                assert bridge.resolve_account(reference).provider == provider
                assert bridge.account_status(account_ref=reference)['configured'][
                    'provider'] == provider
            listed = dispatch(bridge, 'accounts.list', {})
            assert {row['account_ref'] for row in listed} == set(references.values())
            with pytest.raises(BridgeError) as ambiguous:
                bridge.resolve_account('shared')
            assert ambiguous.value.code == 'invalid_request'


def test_atomic_start_rejects_same_provider_duplicate_after_stale_read(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        seed_authenticated_proxy_account(
            bridge.store, _account('saved', 'codex', '  Shared  ', 18332))
        with pytest.raises(BridgeError) as duplicate:
            bridge.store.create_auth_attempt(
                _attempt('new', 'codex', 'shared'), proxy_route=_route(18333))
        assert duplicate.value.code == 'account_name_in_use'
        assert [account.id for account in bridge.accounts()] == ['saved']


def test_historical_same_provider_duplicates_cannot_be_implicitly_reused(
        tmp_path, monkeypatch):
    with Bridge(tmp_path / 'state') as bridge:
        seed_authenticated_proxy_account(
            bridge.store, _account('first', 'codex', 'Shared', 18334))
        seed_authenticated_proxy_account(
            bridge.store, _account('second', 'codex', ' shared ', 18335))
        use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(proxy_responses()))
        with pytest.raises(BridgeError) as duplicate:
            bridge.account_login_start(
                provider='codex', name='SHARED', **_route(18336))
        assert duplicate.value.code == 'account_name_in_use'


def test_atomic_completion_rejects_same_provider_name_claimed_after_start(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        attempt = _attempt('new', 'codex', 'Shared')
        route = _route(18337)
        bridge.store.create_auth_attempt(attempt, proxy_route=route)
        seed_authenticated_proxy_account(
            bridge.store, _account('winner', 'codex', ' shared ', 18338))
        with pytest.raises(BridgeError) as duplicate:
            bind_proxy_account(bridge.store, attempt, route, _observation('codex', 'new'))
        assert duplicate.value.code == 'account_name_in_use'
        assert [account.id for account in bridge.accounts()] == ['winner']


def test_cross_provider_starts_and_completions_can_race_with_same_name(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        attempts = [
            (_attempt('codex-new', 'codex', 'Work'), _route(18339)),
            (_attempt('claude-new', 'claude', ' work '), _route(18340)),
        ]
        start = Barrier(2)

        def create(entry):
            attempt, route = entry
            start.wait(timeout=5)
            return bridge.store.create_auth_attempt(attempt, proxy_route=route)

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert all(created for _, created in pool.map(create, attempts))

        complete = Barrier(2)

        def bind(entry):
            attempt, route = entry
            complete.wait(timeout=5)
            return bind_proxy_account(bridge.store, attempt, route,
                                      _observation(attempt['engine'], attempt['account_id']))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(bind, attempts))
        assert {(account.provider, account.name) for account, _ in results} == {
            ('codex', 'Work'), ('claude', ' work ')}
        assert len(bridge.accounts()) == 2


def test_concurrent_same_provider_starts_admit_only_one_name(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        attempts = [
            (_attempt('first', 'codex', 'Work'), _route(18345)),
            (_attempt('second', 'codex', ' work '), _route(18346)),
        ]
        start = Barrier(2)

        def create(entry):
            attempt, route = entry
            start.wait(timeout=5)
            try:
                bridge.store.create_auth_attempt(attempt, proxy_route=route)
                return 'created'
            except BridgeError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(create, attempts))
        assert sorted(outcomes) == ['busy', 'created']


def test_catalog_and_exclusions_use_distinct_refs_for_duplicate_names(
        tmp_path, monkeypatch):
    with Bridge(tmp_path / 'state') as bridge:
        first = _account('first', 'codex', 'Shared', 18341)
        second = _account('second', 'claude', ' shared ', 18342)
        for account in (first, second):
            monkeypatch.setenv(account.key_env, 'fixture-client')
            seed_authenticated_proxy_account(bridge.store, account)
        expected = {f'id:{first.id}', f'id:{second.id}'}
        catalog = bridge.models(refresh=False)['models'][0]
        assert set(catalog['candidate_account_refs']) == expected
        assert set(catalog['observed_account_refs']) == expected
        assert {row['account_ref'] for row in catalog['account_capabilities']} == expected
        remaining = bridge.routes.candidates('fixture/model',
                                             excluded_account_refs=(f'id:{first.id}',))
        assert [candidate.account_id for candidate in remaining] == [second.id]
        with pytest.raises(BridgeError) as ambiguous:
            bridge.routes.candidates('fixture/model', excluded_account_refs=('shared',))
        assert ambiguous.value.code == 'invalid_request'


def test_typed_reference_resolves_name_id_collision_without_guessing(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        seed_authenticated_proxy_account(
            bridge.store, _account('shared', 'codex', 'Alpha', 18343))
        seed_authenticated_proxy_account(
            bridge.store, _account('second', 'claude', 'shared', 18344))
        with pytest.raises(BridgeError) as ambiguous:
            bridge.resolve_account('shared')
        assert ambiguous.value.code == 'invalid_request'
        assert bridge.account_reference('second') == 'id:second'
        assert bridge.resolve_account('id:shared').id == 'shared'
        assert bridge.resolve_account('id:second').id == 'second'


def test_unknown_typed_reference_never_falls_back_to_a_literal_name(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        seed_authenticated_proxy_account(
            bridge.store, _account('actual', 'codex', 'id:missing', 18347))
        with pytest.raises(BridgeError) as missing:
            bridge.resolve_account('id:missing')
        assert missing.value.code == 'account_not_found'
        assert bridge.resolve_account('id:actual').id == 'actual'
