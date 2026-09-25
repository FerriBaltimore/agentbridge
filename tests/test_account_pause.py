"""Pausing an account fences new routing while retaining durable identity."""

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.commands.account_actions import account_command
from agentbridge.commands.parser import build_parser
from agentbridge.rpc import dispatch
from agentbridge.routing import RouteDecision
from agentbridge.routing.admission import verify_proxy_model
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


def test_pause_is_durable_and_blocks_new_pinned_work(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'first', 18401)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    session = bridge.store.add_session('existing', 'first', str(workspace),
                                       'fixture-model', request_key='stable-create')[0]
    automatic = bridge.store.add_session('automatic', 'first', str(workspace),
                                         'fixture-model', routing_mode='automatic')[0]
    run_id = bridge.store.admit('active', session, 'hello', RunOptions(), None)[0]
    bridge.store.emit(run_id, 'session', {'native_id': 'native-original'})
    paused = dispatch(bridge, 'accounts.pause', {'account_ref': 'first'})
    assert paused['routing']['paused'] is True
    assert dispatch(bridge, 'accounts.pause', {'account_ref': 'first'}) == paused
    assert Bridge(bridge.root).account_status('first')['routing'] == paused['routing']
    assert dispatch(bridge, 'accounts.list', {})[0]['routing'] == paused['routing']
    assert bridge.session('first', str(workspace), model='fixture-model',
                          request_key='stable-create')['replayed'] is True
    assert bridge.store.get('runs', run_id)['state'] == 'starting'
    bridge.store.finish(run_id, 'completed')
    with pytest.raises(BridgeError) as error:
        bridge.store.admit('blocked', session, 'again', RunOptions(), None)
    assert error.value.code == 'account_paused'
    with pytest.raises(BridgeError) as error:
        bridge.store.add_session('blocked-session', 'first', str(workspace), 'fixture-model')
    assert error.value.code == 'account_paused'
    with pytest.raises(BridgeError) as error:
        bridge.store.admit('blocked-automatic', automatic, 'again',
                           RunOptions(model='fixture-model'), None, account_id='first',
                           route_decision=RouteDecision('first', 'fixture-model', 'unknown',
                                                        None, 'healthy', 0, 'selected'))
    assert error.value.code == 'account_paused'
    assert bridge.routes.candidates('fixture-model') == []
    assert bridge.models(account_ref='first')['items'] == []
    assert bridge.account_status('first')['authentication']['status'] == 'active'
    resumed = dispatch(bridge, 'accounts.resume', {'account_ref': 'first'})
    assert resumed['routing'] == {'paused': False, 'paused_at': None}
    assert bridge.store.admit('resumed', session, 'again', RunOptions(), None)[0] == 'resumed'


def test_pausing_after_admission_does_not_reject_worker_verification(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'first', 18403)
    account = bridge.account('first')
    saved = bridge.store.latest_account_observation(account.id)
    bridge.account_pause('first')
    monkeypatch.setattr(bridge.routes, 'observation', lambda *_args, **_kwargs: saved)
    verify_proxy_model(bridge.routes, account, 'fixture-model', refresh=True)


def test_pause_requires_unambiguous_existing_account(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    with pytest.raises(BridgeError) as error:
        bridge.account_pause()
    assert error.value.code == 'invalid_request'
    with pytest.raises(BridgeError) as error:
        bridge.account_pause('missing')
    assert error.value.code == 'account_not_found'


def test_cli_pause_and_resume_use_the_sdk(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'first', 18402)
    parser, _ = build_parser()
    pause = parser.parse_args(['accounts', 'pause', 'first', '--json'])
    assert account_command(bridge, pause)['routing']['paused'] is True
    resume = parser.parse_args(['accounts', 'resume', 'first', '--json'])
    assert account_command(bridge, resume)['routing']['paused'] is False
