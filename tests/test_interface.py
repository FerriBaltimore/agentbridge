import io
import json
import textwrap

import pytest

from agentbridge import Account, Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.cli import rpc


def bridge_for_interface(tmp_path):
    provider = tmp_path / 'fake-provider'
    provider.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json
        print(json.dumps({"type": "thread.started", "thread_id": "native-test"}), flush=True)
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}), flush=True)
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
    '''))
    provider.chmod(0o700)
    bridge = Bridge(tmp_path / 'state')
    bridge.register(Account('test', 'codex', home=str(tmp_path / 'home'), command=(str(provider),), name='Test'))
    return bridge


def test_instances_messages_and_turns_use_the_public_contract(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='Test', workspace_path=str(tmp_path))
    assert instance['state'] == 'active'

    accepted = bridge.message_create(instance['id'], 'hello', idempotency_key='message-1')
    assert accepted['replayed'] is False
    replayed = bridge.message_create(instance['id'], 'hello', idempotency_key='message-1')
    assert replayed['replayed'] is True
    bridge.run(accepted['turn_id']).wait(10)
    assert accepted['instance_id'] == instance['id']
    assert bridge.turns(instance_id=instance['id'])[0]['id'] == accepted['turn_id']
    assert bridge.messages(instance['id'])[0]['role'] == 'user'


def test_instance_version_and_archive_are_durable(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    updated = bridge.instance_update(instance['id'], model='gpt-test', expected_version=1)
    assert updated['model'] == 'gpt-test'
    assert updated['version'] == 2
    archived = bridge.instance_archive(updated['id'], expected_version=2)
    assert archived['state'] == 'archived'


def test_new_rpc_methods_and_error_envelope(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    payloads = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'capabilities.get', 'params': {'engine': 'codex'}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'models.list', 'params': {'engine': 'codex'}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'instances.create',
         'params': {'engine': 'codex', 'account_ref': 'test', 'workspace_path': str(tmp_path)}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'instances.get', 'params': {'instance_id': 'missing'}},
    ]
    out = io.StringIO()
    rpc(bridge, io.StringIO(''.join(json.dumps(item) + '\n' for item in payloads)), out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[0]['result']['operations']['models.list']['support'] == 'native'
    assert responses[1]['result']['source'] == 'static'
    assert responses[2]['result']['state'] == 'active'
    assert responses[3]['error']['data']['code'] == 'not_found'
    assert responses[3]['error']['data']['category'] == 'validation'


def test_capability_filters_are_implemented_or_rejected(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    assert 'parameters' not in bridge.capabilities('codex', include_parameters=False)
    assert bridge.capabilities(account_ref='test')['engine'] == 'codex'
    with pytest.raises(BridgeError) as error:
        bridge.capabilities('codex', refresh=True)
    assert error.value.code == 'unsupported'


def test_message_options_are_validated_before_admission(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    try:
        bridge.message_create(instance['id'], 'hello', context_window='large')
    except Exception as error:
        assert getattr(error, 'code', None) == 'unsupported'
    else:
        raise AssertionError('expected unsupported context window')


def test_public_events_and_instance_export_use_stable_names(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['instance_id'], 'hello')
    bridge.run(accepted['turn_id']).wait(10)

    events = bridge.turn_events(accepted['turn_id'])
    assert events[0]['kind'] == 'message.created'
    assert events[-1]['kind'] == 'run.finished'
    assert events[-1]['engine'] == 'codex'
    exported = bridge.instance_export(instance['instance_id'])
    assert exported.text


def test_rpc_accepts_account_ref_aliases_and_transfer_validation(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'accounts.status',
               'params': {'account_ref': 'test'}}
    out = io.StringIO()
    rpc(bridge, io.StringIO(json.dumps(payload) + '\n'), out)
    response = json.loads(out.getvalue())
    assert response['result']['account_id'] == 'test'

    try:
        bridge.transfer('missing', 'test', validate_only=True)
    except Exception as error:
        assert getattr(error, 'code', None) == 'not_found'
    else:
        raise AssertionError('missing instance must fail validation')


def test_event_limit_returns_one_page(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hello')
    bridge.run(accepted['turn_id']).wait(10)
    assert len(bridge.turn_events(accepted['turn_id'], limit=1)) == 1


def test_last_turn_is_the_most_recent_turn(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    first = bridge.message_create(instance['id'], 'first')
    bridge.run(first['turn_id']).wait(10)
    second = bridge.message_create(instance['id'], 'second')
    bridge.run(second['turn_id']).wait(10)
    assert bridge.instance_get(instance['id'], include_last_turn=True)['last_turn']['id'] == second['turn_id']


def test_permission_response_never_claims_delivery_to_an_unconnected_transport(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hello')
    bridge.run(accepted['turn_id']).wait(10)
    with pytest.raises(BridgeError) as error:
        bridge.permission_respond(accepted['turn_id'], 'missing', 'allow')
    assert error.value.code == 'unsupported'


def test_archived_instance_rejects_new_messages(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    bridge.instance_archive(instance['id'])
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance['id'], 'should fail')
    assert error.value.code == 'instance_archived'


def test_usage_scopes_and_export_options_are_explicit(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hello')
    bridge.run(accepted['turn_id']).wait(10)
    assert bridge.usage('turn', turn_id=accepted['turn_id'])['scope'] == 'turn'
    assert bridge.usage('instance', instance_id=instance['id'])['turns'] == 1
    with pytest.raises(BridgeError) as error:
        bridge.instance_export(instance['id'], include_events=False)
    assert error.value.code == 'unsupported'


def test_capabilities_do_not_overclaim_permission_control(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    capabilities = bridge.capabilities('codex')
    assert capabilities['operations']['permissions.respond']['support'] == 'adapter'
    assert bridge.capabilities('cursor')['operations']['permissions.respond']['support'] == 'unsupported'
    assert capabilities['operations']['usage.get']['support'] == 'adapter'
    assert bridge.capabilities('claude')['operations']['accounts.status']['support'] == 'adapter'
    assert capabilities['parameters']['context_window']['support'] == 'unsupported'
    assert capabilities['parameters']['attachments']['support'] == 'adapter'
    assert bridge.capabilities('cursor')['parameters']['effort']['support'] == 'unsupported'
    assert capabilities['operations']['instances.events']['maturity'] == 'fixture_tested'
    assert capabilities['acceptance']['provider_tested'] is False


def test_invalid_rpc_parameters_use_the_same_error_shape(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'instances.get', 'params': []}
    out = io.StringIO()
    rpc(bridge, io.StringIO(json.dumps(request) + '\n'), out)
    response = json.loads(out.getvalue())
    assert response['error']['data']['code'] == 'invalid_params'
    assert response['error']['data']['category'] == 'validation'


def test_unexpected_rpc_failures_keep_a_safe_error_shape():
    class BrokenBridge:
        def capabilities(self, **_):
            raise RuntimeError('private provider detail')

    out = io.StringIO()
    rpc(BrokenBridge(), io.StringIO('{"jsonrpc":"2.0","id":1,"method":"capabilities","params":{}}\n'), out)
    response = json.loads(out.getvalue())
    assert response['error']['data']['code'] == 'internal_error'
    assert 'private provider detail' not in out.getvalue()


def test_pagination_rejects_negative_values(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    with pytest.raises(BridgeError) as error:
        bridge.instances(limit=-1)
    assert error.value.code == 'invalid_pagination'
    with pytest.raises(BridgeError) as error:
        bridge.models('codex', cursor=-1)
    assert error.value.code == 'invalid_pagination'


def test_stop_and_event_timeouts_reject_negative_values(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hello')
    with pytest.raises(BridgeError) as error:
        bridge.turn_stop(accepted['turn_id'], grace_period_ms=-1)
    assert error.value.code == 'invalid_timeout'
    with pytest.raises(BridgeError) as error:
        bridge.turn_events(accepted['turn_id'], timeout_ms=-1)
    assert error.value.code == 'invalid_timeout'


def test_instances_require_explicit_account_and_are_idempotent(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    with pytest.raises(BridgeError) as error:
        bridge.instance_create(engine='codex', workspace_path=str(tmp_path))
    assert error.value.code == 'account_ref_required'
    first = bridge.instance_create(engine='codex', account_ref='test',
                                   workspace_path=str(tmp_path), idempotency_key='instance-1')
    replay = bridge.instance_create(engine='codex', account_ref='test',
                                    workspace_path=str(tmp_path), idempotency_key='instance-1')
    assert first['id'] == replay['id']
    assert first['replayed'] is False
    assert replay['replayed'] is True
    with pytest.raises(BridgeError) as error:
        bridge.instance_create(engine='codex', account_ref='test',
                               workspace_path=str(tmp_path), model='other',
                               idempotency_key='instance-1')
    assert error.value.code == 'idempotency_conflict'


def test_transfer_is_idempotent_when_the_response_is_lost(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    target_home = tmp_path / 'target-home'
    target_home.mkdir()
    bridge.register(Account('target', 'codex', home=target_home, name='Target'))
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    first = bridge.transfer(instance['id'], 'target', idempotency_key='transfer-1')
    replay = bridge.transfer(instance['id'], 'target', idempotency_key='transfer-1')
    assert first['instance_id'] == replay['instance_id']
    assert replay['replayed'] is True


def test_public_ids_and_instance_event_cursor_are_distinct(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    instance = bridge.instance_create(engine='codex', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hello')
    bridge.run(accepted['turn_id']).wait(10)
    assert accepted['message_id'] != accepted['turn_id']
    events = bridge.instance_events(instance['id'], limit=100)
    assert events[0]['message_id'] == accepted['message_id']
    page = bridge.instance_events(instance['id'], after_seq=events[0]['seq'], limit=1)
    assert len(page) == 1 and page[0]['seq'] == events[1]['seq']


def test_rpc_accounts_are_safe_and_registration_is_blocked(tmp_path):
    bridge = bridge_for_interface(tmp_path)
    out = io.StringIO()
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'accounts.list', 'params': {}}
    rpc(bridge, io.StringIO(json.dumps(request) + '\n'), out)
    account = json.loads(out.getvalue())['result'][0]
    assert set(account) <= {'account_ref', 'name', 'email', 'engine', 'authentication', 'identity', 'reason'}
    assert 'home' not in account and 'command' not in account and 'env_names' not in account
    out = io.StringIO()
    request['id'] = 2
    request['method'] = 'accounts.register'
    request['params'] = {'id': 'bad', 'engine': 'codex', 'home': str(tmp_path)}
    rpc(bridge, io.StringIO(json.dumps(request) + '\n'), out)
    assert json.loads(out.getvalue())['error']['data']['code'] == 'unsupported'
