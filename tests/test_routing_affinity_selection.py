"""Affinity and complete quota windows stay deterministic without live accounts."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agentbridge.errors import BridgeError
from agentbridge.routing.quota import needs_active_refresh, route_quota
from agentbridge.routing.selector import QuotaObservation, RouteCandidate, select_route
from agentbridge.routing.service import RoutingService
from agentbridge.store import Store


MODEL = 'fixture-model'
NOW = 1_000.0


def _candidate(account_id, used=None, *, observed_at=NOW, reset_at=None,
               in_flight=0, cooldown_until=None, health='healthy'):
    quota = () if used is None else (QuotaObservation(
        used, observed_at, MODEL, reset_at, source='fixture', window_id='primary'),)
    return RouteCandidate(account_id, (MODEL,), quota, health=health,
                          in_flight=in_flight, cooldown_until=cooldown_until)


def test_lab_affinity_removes_unnecessary_switches_and_keeps_exhaustion_switch():
    sequence = [(20, 40), (50, 40), (30, 40), (60, 40), (100, 40)]
    baseline = []
    sticky = []
    affinity = None
    for a_used, b_used in sequence:
        rows = (_candidate('a', a_used), _candidate('b', b_used))
        baseline.append(select_route(MODEL, rows, now=NOW).account_id)
        decision = select_route(MODEL, rows, now=NOW, affinity_account_id=affinity)
        sticky.append(decision.account_id)
        affinity = decision.account_id
    assert baseline == ['a', 'b', 'a', 'b', 'b']
    assert sticky == ['a', 'a', 'a', 'a', 'b']
    assert sum(a != b for a, b in zip(baseline, baseline[1:])) == 3
    assert sum(a != b for a, b in zip(sticky, sticky[1:])) == 1
    assert decision.affinity_break_reason == 'quota_exhausted'
    assert decision.affinity_break_evidence['window_id'] == 'primary'


def test_affinity_retains_unknown_quota_and_waits_for_temporary_limits():
    available = _candidate('b', 5)
    assert select_route(MODEL, (_candidate('a'), available), now=NOW,
                        affinity_account_id='a').reason == 'affinity'
    for current, code in ((_candidate('a', 10, in_flight=1), 'account_busy'),
                          (_candidate('a', 10, cooldown_until=NOW + 7), 'rate_limited'),
                          (_candidate('a', 10, health='unhealthy'), 'proxy_binding_unverified'),
                          (_candidate('a', 10, health='unknown'), 'proxy_binding_unverified')):
        with pytest.raises(BridgeError) as caught:
            select_route(MODEL, (current, available), now=NOW, affinity_account_id='a')
        assert caught.value.code == code
        if code in {'account_busy', 'rate_limited'}:
            assert caught.value.retryable is True
    assert select_route(MODEL, (available,), now=NOW,
                        affinity_account_id='a').account_id == 'b'


def test_stale_exhaustion_with_future_reset_requires_refresh_before_reuse():
    stale = _candidate('a', 100, observed_at=NOW - 61, reset_at=NOW + 30)
    with pytest.raises(BridgeError) as caught:
        select_route(MODEL, (stale, _candidate('b', 5)), now=NOW,
                     affinity_account_id='a')
    assert caught.value.code == 'quota_unknown'
    assert caught.value.retryable is True
    assert caught.value.safe_data()['category'] == 'quota'
    assert caught.value.safe_data()['action'] == 'wait'
    reset_passed = _candidate('a', 100, observed_at=NOW - 61, reset_at=NOW - 1)
    decision = select_route(MODEL, (reset_passed, _candidate('b', 5)), now=NOW,
                            affinity_account_id='a')
    assert decision.account_id == 'a' and decision.quota_state == 'unknown'


def test_confirmed_exhaustion_outweighs_cooldown_and_stale_fallback_is_unknown():
    exhausted = _candidate('a', 100, cooldown_until=NOW + 30)
    available = _candidate('b', 40)
    decision = select_route(MODEL, (exhausted, available), now=NOW,
                            affinity_account_id='a')
    assert decision.account_id == 'b'
    assert decision.affinity_break_reason == 'quota_exhausted'

    stale = _candidate('b', 100, observed_at=NOW - 61, reset_at=NOW + 30)
    with pytest.raises(BridgeError) as caught:
        select_route(MODEL, (exhausted, stale), now=NOW,
                     affinity_account_id='a')
    assert caught.value.code == 'quota_unknown'
    assert caught.value.retryable is True
    assert caught.value.details['excluded'] == {
        'unhealthy': 0, 'cooldown': 0, 'busy': 0,
        'quota_exhausted': 1, 'quota_unknown': 1}


def test_exhaustion_evidence_sanitizes_provider_window_label():
    noisy = RouteCandidate('a', (MODEL,), (
        QuotaObservation(100, NOW, MODEL, source='fixture', window_id='résumé'),),
        health='healthy')
    decision = select_route(MODEL, (noisy, _candidate('b', 40)), now=NOW,
                            affinity_account_id='a')
    assert decision.affinity_break_evidence['window_id'] is None


def test_route_quota_preserves_proven_windows_and_marks_ambiguous_scope_unknown():
    snapshot = {'source': 'fixture', 'quota_windows': [
        {'id': 'short', 'scope': 'account', 'model_id': None, 'used_percent': 20,
         'observed_at': NOW, 'resets_at': NOW + 300},
        {'id': 'model', 'scope': 'model', 'model_id': MODEL, 'used_percent': 60,
         'observed_at': NOW, 'resets_at': NOW + 600},
        {'id': 'other', 'scope': 'model', 'model_id': 'other-model', 'used_percent': 100,
         'observed_at': NOW, 'resets_at': NOW + 600},
        {'id': 'opaque', 'scope': 'model_family', 'model_id': None, 'used_percent': 5,
         'observed_at': NOW, 'resets_at': NOW + 600}]}
    windows = route_quota(snapshot, MODEL)
    assert [row.window_id for row in windows] == ['short', 'model', None]
    assert [row.reset_at for row in windows[:2]] == [NOW + 300, NOW + 600]
    decision = select_route(MODEL, (RouteCandidate('a', (MODEL,), windows),), now=NOW)
    assert decision.quota_state == 'unknown' and decision.used_percent is None


def test_active_quota_refreshes_after_reset_or_when_only_other_model_was_read():
    row = {'source': 'cliproxy_upstream_usage', 'scope': 'model', 'model_id': MODEL,
           'used_percent': 40, 'observed_at': NOW - 10, 'resets_at': NOW - 1}
    assert needs_active_refresh({'quota_windows': [row]}, MODEL, now=NOW) is True
    row['resets_at'] = NOW + 50
    assert needs_active_refresh({'quota_windows': [row]}, MODEL, now=NOW) is False
    row['model_id'] = 'other-model'
    assert needs_active_refresh({'quota_windows': [row]}, MODEL, now=NOW) is True


def test_routing_refreshes_missing_quota_once_and_uses_complete_active_windows(monkeypatch):
    account = SimpleNamespace(id='a', provider='codex', proxy_base_url='http://127.0.0.1:1/v1',
                              key_env='LAB_ROUTING_CLIENT_KEY',
                              management_key_env='LAB_ROUTING_MANAGEMENT_KEY',
                              supported_models=(MODEL,))
    monkeypatch.setenv(account.key_env, 'fixture-only')

    class Accounts:
        def list(self):
            return [account]

        def resolve(self, reference):
            assert reference == account.id
            return account

    class Store:
        active = None

        def paused_account_ids(self):
            return set()

        def route_load(self, ids):
            return {key: {'in_flight': 0, 'assigned_turns': 0} for key in ids}

        def latest_usage_observation(self, account_id, *, source):
            assert account_id == account.id
            if source == 'cliproxy_upstream_refresh':
                return None
            assert source == 'cliproxy_upstream_usage'
            return self.active

        def proxy_binding(self, account_id):
            assert account_id == account.id
            return {'binding_fingerprint': 'fixture-binding'}

        def usage_observation(self, account_id, source, scope, data, *, stale):
            self.active = {'source': source, 'data': data}

    class Routes(RoutingService):
        def observation(self, selected, *, refresh=False, now=None, include_catalog=True):
            assert selected.id == account.id
            return {'data': {'account_id': account.id, 'provider': account.provider,
                             'status': 'active', 'disabled': False, 'unavailable': False,
                             'binding_verified': True, 'models': [{'id': MODEL}],
                             'quota_windows': []}}

    called = []
    current = datetime.now(timezone.utc).isoformat()

    class Management:
        def __init__(self, route, key, *, timeout):
            assert timeout == 8

        def fetch_quota(self, fingerprint):
            called.append(fingerprint)
            return {'source': 'cliproxy_upstream_usage', 'quota_windows': [
                {'id': 'primary', 'scope': 'account', 'model_id': None,
                 'used_percent': 25, 'observed_at': current, 'resets_at': None},
                {'id': 'secondary', 'scope': 'account', 'model_id': None,
                 'used_percent': 80, 'observed_at': current, 'resets_at': None}]}

    monkeypatch.setattr('agentbridge.routing.service.ManagementClient', Management)
    routes = Routes(Store(), Accounts())
    first = routes.candidates(MODEL, refresh=True)[0]
    second = routes.candidates(MODEL, refresh=True)[0]
    assert [row.used_percent for row in first.quota] == [25, 80]
    assert [row.source for row in second.quota] == ['cliproxy_upstream_usage'] * 2
    assert called == ['fixture-binding']


def test_service_observes_only_usable_affinity_until_confirmed_exhaustion(tmp_path, monkeypatch):
    accounts = [SimpleNamespace(
        id=value, provider='codex', proxy_base_url='http://127.0.0.1:1/v1',
        key_env=f'LAB_{value.upper()}_KEY', management_key_env=f'LAB_{value.upper()}_MANAGEMENT',
        supported_models=(MODEL,)) for value in ('a', 'b')]
    for account in accounts:
        monkeypatch.setenv(account.key_env, 'fixture-only')

    class Accounts:
        def list(self):
            return accounts

        def resolve(self, reference):
            return next(row for row in accounts if row.id == reference)

    class Routes(RoutingService):
        seen = []
        utilization = {'a': 25, 'b': 10}

        def observation(self, account, *, refresh=False, now=None, include_catalog=True):
            self.seen.append(account.id)
            return {'data': {'account_id': account.id, 'provider': account.provider,
                             'status': 'active', 'disabled': False, 'unavailable': False,
                             'binding_verified': True, 'models': [{'id': MODEL}]}}

        def _quota_snapshot(self, account, saved, *, refresh_active=False):
            return {'source': 'fixture', 'quota_windows': [{
                'id': 'primary', 'scope': 'model', 'model_id': MODEL,
                'used_percent': self.utilization[account.id],
                'observed_at': datetime.now(timezone.utc).isoformat(),
                'resets_at': None}]}

    routes = Routes(Store(tmp_path), Accounts())
    assert routes.select(MODEL, affinity_account_id='a').account_id == 'a'
    assert routes.seen == ['a']
    routes.seen.clear()
    routes.utilization['a'] = 100
    decision = routes.select(MODEL, affinity_account_id='a')
    assert decision.account_id == 'b'
    assert routes.seen == ['a', 'b']
    assert decision.affinity_break_evidence['window_id'] == 'primary'


def test_failed_active_refresh_is_throttled_between_services_in_one_process(tmp_path, monkeypatch):
    account = SimpleNamespace(id='a', provider='codex',
                              proxy_base_url='http://127.0.0.1:1/v1',
                              key_env='LAB_ROUTING_FAILURE_CLIENT_KEY',
                              management_key_env='LAB_ROUTING_FAILURE_MANAGEMENT_KEY',
                              supported_models=(MODEL,))
    monkeypatch.setenv(account.key_env, 'fixture-only')

    class Accounts:
        def list(self):
            return [account]

    class Routes(RoutingService):
        def observation(self, selected, *, refresh=False, now=None, include_catalog=True):
            return {'data': {'account_id': selected.id, 'provider': selected.provider,
                             'status': 'active', 'disabled': False, 'unavailable': False,
                             'binding_verified': True, 'models': [{'id': MODEL}],
                             'quota_windows': []}}

    calls = []

    class Management:
        def __init__(self, route, key, *, timeout):
            assert timeout == 8

        def fetch_quota(self, fingerprint):
            calls.append(fingerprint)
            raise BridgeError('upstream_quota_unavailable', 'Private fixture body')

    store = Store(tmp_path)
    monkeypatch.setattr(store, 'proxy_binding', lambda account_id: {
        'binding_fingerprint': 'fixture-binding'})
    monkeypatch.setattr('agentbridge.routing.service.ManagementClient', Management)
    routes = Routes(store, Accounts())
    assert routes.candidates(MODEL, refresh=True)[0].quota == ()
    assert Routes(store, Accounts()).candidates(MODEL, refresh=True)[0].quota == ()
    assert calls == ['fixture-binding']
    assert store.usage_history('a') == []
