import io
import json
import textwrap
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.cli import rpc
from fixtures.test_proxy_account_fixture import proxy_account, seed_authenticated_proxy_account


@contextmanager
def management_server(account_id):
    class Handler(BaseHTTPRequestHandler):
        def handle_get(self):
            if self.path == '/v0/management/auth-files':
                body = {'files': [{'name': 'fixture.json', 'provider': 'codex',
                    'auth_index': f'fixture-{account_id}', 'account_type': 'oauth',
                    'id_token': {'chatgpt_account_id': account_id}, 'source': 'file',
                    'runtime_only': False, 'status': 'active', 'disabled': False,
                    'unavailable': False, 'cooldowns': []}]}
            elif self.path == '/v0/management/config':
                body = {key: [] for key in ('gemini-api-key', 'interactions-api-key',
                    'claude-api-key', 'codex-api-key', 'xai-api-key', 'meta-api-key',
                    'vertex-api-key', 'openai-compatibility')}
                body['plugins'] = {'enabled': False}
            elif self.path == '/v0/management/auth-files/models?name=fixture.json':
                body = {'models': [{'id': 'fixture-model'}, {'id': 'gpt-test'}]}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *_):
            pass

    setattr(Handler, 'do_GET', Handler.handle_get)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def add_proxy_account(bridge, account_id, port, *, name, command):
    account = replace(proxy_account(account_id, port, provider='codex'),
                      name=name, command=(str(command),),
                      supported_models=('fixture-model', 'gpt-test'))
    account = seed_authenticated_proxy_account(bridge.store, account, observe_local=True)
    assert bridge.routes.observe(account)['status'] == 'active'


@pytest.fixture
def bridge_for_interface(tmp_path, monkeypatch):
    provider = tmp_path / 'fake-provider'
    provider.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json
        print(json.dumps({"type": "thread.started", "thread_id": "native-test"}), flush=True)
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}), flush=True)
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
    '''))
    provider.chmod(0o700)
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'local-fixture-client-key')
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', 'local-fixture-management-key')
    with management_server('test') as port:
        bridge = Bridge(tmp_path / 'state')
        add_proxy_account(bridge, 'test', port, name='Test', command=provider)
        yield bridge
        bridge.close(cancel=True)


def test_instances_messages_and_turns_use_the_public_contract(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='Test', workspace_path=str(tmp_path))
    assert instance['state'] == 'active'

    accepted = bridge.message_create(instance['id'], 'hello', idempotency_key='message-1')
    assert accepted['replayed'] is False
    replayed = bridge.message_create(instance['id'], 'hello', idempotency_key='message-1')
    assert replayed['replayed'] is True
    bridge.run(accepted['turn_id']).wait(10)
    assert accepted['instance_id'] == instance['id']
    assert bridge.turns(instance_id=instance['id'])[0]['id'] == accepted['turn_id']
    assert bridge.messages(instance['id'])[0]['role'] == 'user'


def test_instance_version_and_archive_are_durable(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='test', workspace_path=str(tmp_path))
    updated = bridge.instance_update(instance['id'], model='gpt-test', expected_version=1)
    assert updated['model'] == 'gpt-test'
    assert updated['version'] == 2
    archived = bridge.instance_archive(updated['id'], expected_version=2)
    assert archived['state'] == 'archived'


def test_new_rpc_methods_and_error_envelope(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    payloads = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'capabilities.get', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'models.list', 'params': {}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'instances.create',
         'params': {'model': 'fixture-model', 'account_ref': 'test',
                    'workspace_path': str(tmp_path)}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'instances.get', 'params': {'instance_id': 'missing'}},
    ]
    out = io.StringIO()
    rpc(bridge, io.StringIO(''.join(json.dumps(item) + '\n' for item in payloads)), out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[0]['result']['operations']['models.list']['support'] == 'adapter'
    assert responses[1]['result']['source'] == 'agentbridge_routing'
    assert responses[2]['result']['state'] == 'active'
    assert responses[3]['error']['data']['code'] == 'not_found'
    assert responses[3]['error']['data']['category'] == 'validation'


def test_capability_filters_are_implemented_or_rejected(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    assert 'parameters' not in bridge.capabilities(include_parameters=False)
    assert bridge.capabilities(account_ref='test')['execution_engine'] == 'codex'
    with pytest.raises(BridgeError) as error:
        bridge.capabilities(refresh=True)
    assert error.value.code == 'unsupported'
    with pytest.raises(BridgeError) as error:
        bridge.capabilities(engine='codex')
    assert error.value.code == 'unsupported_parameter'


def test_invalid_context_window_is_rejected_before_admission(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='test', workspace_path=str(tmp_path))
    try:
        bridge.message_create(instance['id'], 'hello', context_window='large')
    except Exception as error:
        assert getattr(error, 'code', None) == 'invalid_context_window'
    else:
        raise AssertionError('expected invalid context window')


def test_public_events_and_instance_export_use_stable_names(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['instance_id'], 'hello')
    bridge.run(accepted['turn_id']).wait(10)

    events = bridge.turn_events(accepted['turn_id'])
    assert events[0]['kind'] == 'message.created'
    assert events[-1]['kind'] == 'run.finished'
    assert events[-1]['engine'] == 'codex'
    exported = bridge.instance_export(instance['instance_id'])
    assert exported.text


def test_rpc_accepts_account_ref_aliases_and_transfer_validation(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
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


def test_event_limit_returns_one_page(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hello')
    bridge.run(accepted['turn_id']).wait(10)
    assert len(bridge.turn_events(accepted['turn_id'], limit=1)) == 1


def test_last_turn_is_the_most_recent_turn(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='test', workspace_path=str(tmp_path))
    first = bridge.message_create(instance['id'], 'first')
    bridge.run(first['turn_id']).wait(10)
    second = bridge.message_create(instance['id'], 'second')
    bridge.run(second['turn_id']).wait(10)
    assert bridge.instance_get(instance['id'], include_last_turn=True)['last_turn']['id'] == second['turn_id']


def test_permission_response_never_claims_delivery_to_an_unconnected_transport(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hello')
    bridge.run(accepted['turn_id']).wait(10)
    with pytest.raises(BridgeError) as error:
        bridge.permission_respond(accepted['turn_id'], 'missing', 'allow')
    assert error.value.code == 'unsupported'


def test_archived_instance_rejects_new_messages(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='test', workspace_path=str(tmp_path))
    bridge.instance_archive(instance['id'])
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance['id'], 'should fail')
    assert error.value.code == 'instance_archived'


def test_usage_scopes_and_export_options_are_explicit(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hello')
    bridge.run(accepted['turn_id']).wait(10)
    assert bridge.usage('turn', turn_id=accepted['turn_id'])['scope'] == 'turn'
    assert bridge.usage('instance', instance_id=instance['id'])['turns'] == 1
    with pytest.raises(BridgeError) as error:
        bridge.instance_export(instance['id'], include_events=False)
    assert error.value.code == 'unsupported'


def test_capabilities_do_not_overclaim_permission_control(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    capabilities = bridge.capabilities()
    assert capabilities['operations']['permissions.respond']['support'] == 'adapter'
    assert capabilities['operations']['usage.get']['support'] == 'adapter'
    assert capabilities['operations']['accounts.status']['support'] == 'adapter'
    assert capabilities['operations']['error_cases.diagnose']['support'] == 'unsupported'
    assert capabilities['parameters']['context_window']['support'] == 'adapter'
    assert capabilities['parameters']['attachments']['support'] == 'adapter'
    assert capabilities['parameters']['effort']['support'] == 'adapter'
    assert capabilities['parameters']['effort']['limitations'] == ['provider_model_may_ignore_effort']
    assert [item['value'] for item in capabilities['parameters']['permission_mode']['values']] == [
        'dontAsk', 'default']
    assert capabilities['operations']['instances.events']['maturity'] == 'fixture_tested'
    assert capabilities['runtime_provider_support_verified'] is False


def test_invalid_rpc_parameters_use_the_same_error_shape(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
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


def test_pagination_rejects_negative_values(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    with pytest.raises(BridgeError) as error:
        bridge.instances(limit=-1)
    assert error.value.code == 'invalid_pagination'
    with pytest.raises(BridgeError) as error:
        bridge.models(cursor=-1)
    assert error.value.code == 'invalid_pagination'


def test_stop_and_event_timeouts_reject_negative_values(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hello')
    with pytest.raises(BridgeError) as error:
        bridge.turn_stop(accepted['turn_id'], grace_period_ms=-1)
    assert error.value.code == 'invalid_timeout'
    with pytest.raises(BridgeError) as error:
        bridge.turn_events(accepted['turn_id'], timeout_ms=-1)
    assert error.value.code == 'invalid_timeout'


def test_instances_require_a_model_or_explicit_account_and_are_idempotent(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    with pytest.raises(BridgeError) as error:
        bridge.instance_create(workspace_path=str(tmp_path))
    assert error.value.code == 'model_required'
    first = bridge.instance_create(model='fixture-model', account_ref='test',
                                   workspace_path=str(tmp_path), idempotency_key='instance-1')
    replay = bridge.instance_create(model='fixture-model', account_ref='test',
                                    workspace_path=str(tmp_path), idempotency_key='instance-1')
    assert first['id'] == replay['id']
    assert first['replayed'] is False
    assert replay['replayed'] is True
    with pytest.raises(BridgeError) as error:
        bridge.instance_create(model='gpt-test', account_ref='test',
                               workspace_path=str(tmp_path),
                               idempotency_key='instance-1')
    assert error.value.code == 'idempotency_conflict'


def test_transfer_is_idempotent_when_the_response_is_lost(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    with management_server('target') as port:
        add_proxy_account(bridge, 'target', port, name='Target',
                          command=tmp_path / 'fake-provider')
        instance = bridge.instance_create(model='fixture-model', account_ref='test',
                                          workspace_path=str(tmp_path))
        first = bridge.transfer(instance['id'], 'target', idempotency_key='transfer-1')
        replay = bridge.transfer(instance['id'], 'target', idempotency_key='transfer-1')
        assert first['instance_id'] == replay['instance_id']
        assert replay['replayed'] is True


def test_public_ids_and_instance_event_cursor_are_distinct(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='test', workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hello')
    bridge.run(accepted['turn_id']).wait(10)
    assert accepted['message_id'] != accepted['turn_id']
    events = bridge.instance_events(instance['id'], limit=100)
    assert events[0]['message_id'] == accepted['message_id']
    page = bridge.instance_events(instance['id'], after_seq=events[0]['seq'], limit=1)
    assert len(page) == 1 and page[0]['seq'] == events[1]['seq']


def test_rpc_accounts_are_safe_and_registration_is_blocked(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    out = io.StringIO()
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'accounts.list', 'params': {}}
    rpc(bridge, io.StringIO(json.dumps(request) + '\n'), out)
    account = json.loads(out.getvalue())['result'][0]
    assert set(account) <= {'account_ref', 'name', 'email', 'provider', 'supported_models',
                            'authentication', 'identity', 'reason'}
    assert account['provider'] == 'codex' and account['supported_models'] == ['fixture-model', 'gpt-test']
    assert 'home' not in account and 'command' not in account and 'env_names' not in account
    out = io.StringIO()
    request['id'] = 2
    request['method'] = 'accounts.register'
    request['params'] = {'id': 'bad', 'engine': 'codex', 'home': str(tmp_path)}
    rpc(bridge, io.StringIO(json.dumps(request) + '\n'), out)
    assert json.loads(out.getvalue())['error']['data']['code'] == 'authentication_required'
