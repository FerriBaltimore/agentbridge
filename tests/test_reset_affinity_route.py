"""A pending earned reset cannot silently move an automatic conversation."""

import json
import time

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.queueing.persistence import QueueStore
from agentbridge.queueing.worker import Dispatcher
from agentbridge.routing import RouteDecision
from test_routing_affinity_store import MODEL, prepared


def _pending_reset(store):
    binding = store.proxy_binding('a')
    snapshot = store.save_reset_observation(
        'a', binding, {'status': 'available', 'available_count': 1, 'credits': None},
        store.reset_generation('a'))
    store.begin_reset_attempt('a', 'fixture-reset-key', snapshot['observation_ref'],
                              None, binding)


def _switch_decision(observed_at=None):
    evidence = {'account_id': 'a', 'source': 'fixture', 'window_id': 'primary',
                'observed_at': time.time() if observed_at is None else observed_at,
                'reset_at': None, 'used_percent': 100, 'limit_reached': True}
    return RouteDecision('b', MODEL, 'unknown', None, 'healthy', 0, 'quota_unknown',
                         'quota_exhausted', evidence)


def test_pending_affinity_rejects_selection_before_probing_other_accounts(
        tmp_path, monkeypatch):
    store = prepared(tmp_path)
    _pending_reset(store)
    bridge = Bridge(store.root)
    monkeypatch.setattr(bridge.routes, 'observation',
                        lambda *_args, **_kwargs: pytest.fail('No account should be probed.'))

    with pytest.raises(BridgeError) as error:
        bridge.routes.select(MODEL, affinity_account_id='a')
    assert error.value.code == 'reset_pending'


def test_pending_affinity_blocks_selected_switch_at_atomic_admission(tmp_path):
    store = prepared(tmp_path)
    store.add_session('instance', 'a', str(tmp_path), MODEL, routing_mode='automatic')
    decision = _switch_decision()
    _pending_reset(store)

    with pytest.raises(BridgeError) as error:
        store.admit('other-account', 'instance', 'fixture prompt', RunOptions(model=MODEL),
                    None, account_id='b', route_decision=decision)
    assert error.value.code == 'reset_pending'
    assert store.session_run_count('instance') == 0
    assert store.routing('instance')['affinity_account_id'] == 'a'


def test_switch_requires_break_evidence_and_rejects_pre_reset_quota(tmp_path):
    store = prepared(tmp_path)
    store.add_session('instance', 'a', str(tmp_path), MODEL, routing_mode='automatic')
    unsupported = RouteDecision('b', MODEL, 'unknown', None, 'healthy', 0, 'quota_unknown')
    with pytest.raises(BridgeError) as missing:
        store.admit('unsupported', 'instance', 'fixture prompt', RunOptions(model=MODEL),
                    None, account_id='b', route_decision=unsupported)
    assert missing.value.code == 'invalid_request'

    forged = RouteDecision('b', MODEL, 'unknown', None, 'healthy', 0, 'quota_unknown',
                           'explicit_exclusion', {'account_id': 'a'})
    with pytest.raises(BridgeError) as excluded:
        store.admit('forged-exclusion', 'instance', 'fixture prompt',
                    RunOptions(model=MODEL), None, account_id='b', route_decision=forged)
    assert excluded.value.code == 'invalid_request'

    old_decision = _switch_decision(observed_at=time.time() - 1)
    _pending_reset(store)
    store.finish_reset_attempt('fixture-reset-key', 'reset', 1)
    with pytest.raises(BridgeError) as stale:
        store.admit('stale-break', 'instance', 'fixture prompt', RunOptions(model=MODEL),
                    None, account_id='b', route_decision=old_decision)
    assert stale.value.code == 'quota_unknown'
    assert store.session_run_count('instance') == 0


def test_queued_message_blocks_without_starting_turn_or_changing_affinity(tmp_path):
    store = prepared(tmp_path)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store.add_session('instance', 'a', str(workspace), MODEL, routing_mode='automatic')
    queue = QueueStore(store)
    message_id, created = queue.add('instance', 'queued prompt', RunOptions(model=MODEL),
                                    (), None, 'fixture-digest')
    assert created
    queue.activate('instance', message_id)
    _pending_reset(store)

    dispatcher = Dispatcher(store.root, 'instance')
    dispatcher.advance()
    item = queue.get(message_id)
    assert item['state'] == 'blocked' and item['error'] == 'reset_pending'
    assert queue.snapshot('instance')['paused'] is True
    assert store.session_run_count('instance') == 0
    assert store.routing('instance')['affinity_account_id'] == 'a'


@pytest.mark.parametrize('state,reason', [
    ('paused', 'account_paused'), ('retired', 'account_retired'),
])
def test_non_reset_ineligibility_keeps_explicit_affinity_break(
        tmp_path, monkeypatch, state, reason):
    store = prepared(tmp_path)
    store.add_session('instance', 'a', str(tmp_path), MODEL, routing_mode='automatic')
    bridge = Bridge(store.root)
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'fixture-only')
    monkeypatch.setattr(bridge.routes, 'observation',
                        lambda account, **_kwargs: store.latest_account_observation(account.id))
    monkeypatch.setattr(bridge.routes, '_quota_snapshot',
                        lambda *_args, **_kwargs: {'source': 'fixture', 'quota_windows': []})
    if state == 'paused':
        store.set_account_paused('a', True)
    else:
        store.retire_account('a')

    decision = bridge.routes.select(MODEL, affinity_account_id='a')
    assert decision.account_id == 'b'
    assert decision.affinity_break_reason == reason
    assert decision.affinity_break_evidence == {'account_id': 'a'}
    store.admit('replacement', 'instance', 'fixture prompt', RunOptions(model=MODEL),
                None, account_id='b', route_decision=decision)
    assert store.routing('instance')['affinity_account_id'] == 'b'


def test_explicit_context_limit_can_break_affinity_with_matching_evidence(
        tmp_path, monkeypatch):
    store = prepared(tmp_path)
    store.add_session('instance', 'a', str(tmp_path), MODEL, routing_mode='automatic')
    for account_id, maximum in [('a', 100), ('b', 200)]:
        saved = store.latest_account_observation(account_id)
        data = {**saved['data'], 'model_metadata': {
            MODEL: {'max_context_window': maximum}}}
        store.account_observation(account_id, 'cliproxy_management', 'active', data)
    bridge = Bridge(store.root)
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'fixture-only')
    monkeypatch.setattr(bridge.routes, 'observation',
                        lambda account, **_kwargs: store.latest_account_observation(account.id))
    monkeypatch.setattr(bridge.routes, '_quota_snapshot',
                        lambda *_args, **_kwargs: {'source': 'fixture', 'quota_windows': []})

    decision = bridge.routes.select(MODEL, affinity_account_id='a', context_window=150)
    assert decision.account_id == 'b'
    assert decision.affinity_break_reason == 'context_window_incompatible'
    assert decision.affinity_break_evidence == {'account_id': 'a', 'context_window': 150}
    store.admit('larger-context', 'instance', 'fixture prompt',
                RunOptions(model=MODEL, context_window=150), None,
                account_id='b', route_decision=decision)
    assert store.routing('instance')['affinity_account_id'] == 'b'


@pytest.mark.parametrize('change,reason', [
    ('model', 'model_incompatible'), ('provider', 'provider_incompatible'),
])
def test_explicit_policy_change_preserves_verified_affinity_break(
        tmp_path, monkeypatch, change, reason):
    store = prepared(tmp_path)
    store.add_session('instance', 'a', str(tmp_path), MODEL, routing_mode='automatic',
                      routing_provider='fixture')
    with store.connect() as db:
        row = db.execute("SELECT config FROM accounts WHERE id='a'").fetchone()
        config = json.loads(row['config'])
        if change == 'model':
            config['supported_models'] = ['other-model']
        else:
            config['provider'] = 'claude'
        db.execute("UPDATE accounts SET config=? WHERE id='a'", (json.dumps(config),))
    bridge = Bridge(store.root)
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'fixture-only')
    monkeypatch.setattr(bridge.routes, 'observation',
                        lambda account, **_kwargs: store.latest_account_observation(account.id))
    monkeypatch.setattr(bridge.routes, '_quota_snapshot',
                        lambda *_args, **_kwargs: {'source': 'fixture', 'quota_windows': []})

    decision = bridge.routes.select(MODEL, provider='fixture', affinity_account_id='a')
    assert decision.account_id == 'b'
    assert decision.affinity_break_reason == reason
    store.admit('replacement', 'instance', 'fixture prompt', RunOptions(model=MODEL),
                None, account_id='b', route_decision=decision)
    assert store.routing('instance')['affinity_account_id'] == 'b'
