"""One chat retains its Codex history across proxy routes and explicit recovery."""

from contextlib import ExitStack
import json
import time

import pytest

from agentbridge import Account, Bridge
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from test_v2_routing_integration import _fake_codex, management_server


@pytest.fixture
def routed(tmp_path, monkeypatch):
    fixture = tmp_path / 'codex-fixture'
    _fake_codex(fixture)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    states = {
        'alpha': {'used': 10, 'identity': 'fixture-alpha', 'provider': 'codex',
                  'models': ['lab-model', 'lab-other']},
        'beta': {'used': 10, 'identity': 'fixture-beta', 'provider': 'claude',
                 'models': ['lab-claude']},
    }
    with ExitStack() as stack:
        bridge = Bridge(tmp_path / 'state')
        for name, state in states.items():
            endpoint = stack.enter_context(management_server(state))
            key_env = f'LAB_PROXY_{name.upper()}'
            management_env = f'LAB_MANAGEMENT_{name.upper()}'
            monkeypatch.setenv(key_env, f'local-fixture-{name}')
            monkeypatch.setenv(management_env, f'local-management-{name}')
            seed_authenticated_proxy_account(bridge.store, Account(
                name, 'codex', name=name, key_env=key_env, management_key_env=management_env,
                proxy_base_url=endpoint, provider=state['provider'],
                supported_models=tuple(state['models']), command=(str(fixture),)),
                observe_local=True)
        instance = bridge.instance_create(
            model='lab-model', account_ref='alpha', routing_mode='automatic', workspace_path=workspace)
        yield bridge, instance['id']
        bridge.close(cancel=True)


def completed(bridge, instance, prompt, permission_mode):
    accepted = bridge.message_create(instance, prompt, permission_mode=permission_mode)
    run = bridge.run(accepted['turn_id'])
    assert run.wait(10)['state'] == 'completed', run.snapshot
    return json.loads(run.text)


@pytest.mark.parametrize('permission_mode', ['dontAsk', 'default'], ids=['exec', 'app-server'])
def test_model_account_and_provider_changes_resume_original_codex_history(routed, permission_mode):
    bridge, instance = routed
    first = completed(bridge, instance, 'remember this original request', permission_mode)
    assert first['resumed'] is False and first['previous_prompts'] == []
    native_id = bridge.instance_get(instance)['native_session_id']

    bridge.instance_update(instance, model='lab-other')
    second = completed(bridge, instance, 'change the model', permission_mode)
    assert second['model'] == 'lab-other' and second['account'] == 'alpha'
    assert second['resumed'] is True
    assert second['previous_prompts'] == ['remember this original request']

    bridge.instance_update(instance, model='lab-claude', provider='claude')
    third = completed(Bridge(bridge.root), instance, 'change the provider', permission_mode)
    assert third['model'] == 'lab-claude' and third['account'] == 'beta'
    assert third['resumed'] is True
    assert third['previous_prompts'] == ['remember this original request', 'change the model']

    bridge.instance_update(instance, model='lab-model', account_ref='alpha', routing_mode='pinned')
    fourth = completed(bridge, instance, 'return to the initial route', permission_mode)
    assert fourth['model'] == 'lab-model' and fourth['account'] == 'alpha'
    assert fourth['resumed'] is True
    assert fourth['previous_prompts'] == [
        'remember this original request', 'change the model', 'change the provider']
    for result in (first, second, third, fourth):
        assert result['native_id'] == native_id
        assert result['portable'] is False
    assert bridge.instance_get(instance)['native_session_id'] == native_id
    assert all(account.engine == 'codex' for account in bridge.accounts())
    assert all(not event['data']['portable_context_used'] for event in bridge.instance_events(instance)
               if event['kind'] == 'route.selected')


@pytest.mark.parametrize('permission_mode', ['dontAsk', 'default'], ids=['exec', 'app-server'])
@pytest.mark.parametrize('terminal', ['failed', 'interrupted', 'cancelled'])
def test_first_unsuccessful_turn_keeps_native_history_for_explicit_resume(
        routed, permission_mode, terminal):
    bridge, instance = routed
    prompt = {'failed': 'fail', 'interrupted': 'interrupt', 'cancelled': 'hold:never'}[terminal]
    accepted = bridge.message_create(instance, prompt, permission_mode=permission_mode)
    run = bridge.run(accepted['turn_id'])
    if terminal == 'cancelled':
        deadline = time.monotonic() + 10
        while not run.text and time.monotonic() < deadline:
            time.sleep(.02)
        assert run.text
        bridge.turn_stop(run.id, wait=True)
    assert run.wait(10)['state'] == terminal
    native_id = bridge.instance_get(instance)['native_session_id']
    assert native_id == 'native-alpha'

    other = Bridge(bridge.root)
    other.recover(instance_id=instance)
    assert len(other.runs()) == 1
    assert other.instance_get(instance)['native_session_id'] == native_id
    resumed = other.run(run.id).resume('continue explicitly')
    assert resumed.wait(10)['state'] == 'completed', resumed.snapshot
    result = json.loads(resumed.text)
    assert result['resumed'] is True and result['portable'] is False
    assert result['native_id'] == native_id
    assert result['previous_prompts'] == [prompt]
    assert len(other.runs()) == 2


@pytest.mark.parametrize('permission_mode', ['dontAsk', 'default'], ids=['exec', 'app-server'])
def test_unavailable_native_history_never_starts_a_replacement(routed, permission_mode):
    bridge, instance = routed
    completed(bridge, instance, 'first request', permission_mode)
    history = bridge.root / 'codex-runtime' / instance / 'fixture-native-session.json'
    history.unlink()
    accepted = bridge.message_create(instance, 'continue', permission_mode=permission_mode)
    run = bridge.run(accepted['turn_id'])
    assert run.wait(10)['state'] != 'completed'
    assert bridge.instance_get(instance)['native_session_id'] == 'native-alpha'
    assert not history.exists()
    assert len(bridge.runs()) == 2


def test_exec_replacement_thread_is_rejected_without_publishing_its_answer(routed):
    bridge, instance = routed
    completed(bridge, instance, 'first request', 'dontAsk')
    marker = bridge.root / 'codex-runtime' / instance / 'fixture-diverge'
    marker.touch()
    accepted = bridge.message_create(instance, 'continue')
    run = bridge.run(accepted['turn_id'])
    outcome = run.wait(10)
    assert outcome['state'] != 'completed'
    assert outcome['error'] == 'native_session_diverged'
    assert run.text == ''
    assert bridge.instance_get(instance)['native_session_id'] == 'native-alpha'
    assert not any(event['kind'] == 'thread.started' for event in bridge.turn_events(run.id))
    assert len(bridge.runs()) == 2


@pytest.mark.parametrize('resumed', [False, True], ids=['first-turn', 'resumed-turn'])
def test_exec_completion_without_native_confirmation_is_interrupted(routed, resumed):
    bridge, instance = routed
    if resumed:
        completed(bridge, instance, 'first request', 'dontAsk')
    original_native_id = bridge.instance_get(instance)['native_session_id']
    home = bridge.root / 'codex-runtime' / instance
    home.mkdir(parents=True, exist_ok=True)
    (home / 'fixture-omit-native').touch()

    accepted = bridge.message_create(instance, 'answer without confirming the thread')
    run = bridge.run(accepted['turn_id'])
    outcome = run.wait(10)
    assert outcome['state'] == 'interrupted'
    assert outcome['error'] == 'native_session_missing'
    assert bridge.instance_get(instance)['native_session_id'] == original_native_id
    assert not any(event['kind'] == 'thread.started' for event in bridge.turn_events(run.id))
    history = json.loads((home / 'fixture-native-session.json').read_text())
    assert history['history'][-1] == 'answer without confirming the thread'
    bridge.recover(instance_id=instance)
    assert len(bridge.runs()) == (2 if resumed else 1)
