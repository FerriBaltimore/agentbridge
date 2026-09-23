import base64
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
import time

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.permissions import Permissions
from fixtures.test_proxy_account_fixture import proxy_account, seed_authenticated_proxy_account

PICTURE = {'type': 'image', 'media_type': 'image/png',
           'data': base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()}
FIXTURE = Path(__file__).parent / 'fixtures' / 'test_duplex_provider.py'


@contextmanager
def management_server():
    class Handler(BaseHTTPRequestHandler):
        def handle_get(self):
            if self.path == '/v0/management/auth-files':
                body = {'files': [{'name': 'fixture.json', 'provider': 'codex',
                    'auth_index': 'fixture-test', 'account_type': 'oauth',
                    'id_token': {'chatgpt_account_id': 'fixture-test'}, 'source': 'file',
                    'runtime_only': False, 'status': 'active', 'disabled': False,
                    'unavailable': False, 'cooldowns': []}]}
            elif self.path == '/v0/management/config':
                body = {key: [] for key in ('gemini-api-key', 'interactions-api-key',
                    'claude-api-key', 'codex-api-key', 'xai-api-key', 'meta-api-key',
                    'vertex-api-key', 'openai-compatibility')}
                body['plugins'] = {'enabled': False}
            elif self.path == '/v0/management/auth-files/models?name=fixture.json':
                body = {'models': [{'id': 'fixture-model'}]}
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


@pytest.fixture
def setup_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'fixture-client-key')
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', 'fixture-management-key')
    with management_server() as port:
        bridge = Bridge(tmp_path / 'state')

        def create(*, native_args=()):
            account = replace(proxy_account('test', port, provider='codex'),
                              command=(sys.executable, str(FIXTURE), *native_args))
            account = seed_authenticated_proxy_account(bridge.store, account,
                                                       observe_local=True)
            assert bridge.routes.observe(account)['status'] == 'active'
            instance = bridge.instance_create(account_ref='test', model='fixture-model',
                                              workspace_path=str(tmp_path))
            return bridge, instance['id']

        yield create
        bridge.close(cancel=True)


def pending(bridge, turn):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        events = [e for e in bridge.turn_events(turn) if e['kind'] == 'permission.required']
        if events:
            return events[0]['data']['permission_id']
        time.sleep(0.05)
    raise AssertionError(bridge.run(turn).snapshot)


@pytest.mark.parametrize('decision', ['allow', 'deny'])
def test_permission_roundtrip_after_client_restart(setup_proxy, decision):
    bridge, instance = setup_proxy()
    turn = bridge.message_create(instance, 'hello', permission_mode='default')['turn_id']
    request = pending(bridge, turn)
    other = Bridge(bridge.root)
    with pytest.raises(BridgeError, match='matching'):
        Permissions(other.store).respond('wrong-turn', request, 'allow')
    result = other.permission_respond(turn, request, decision)
    assert result['state'] == 'queued'
    assert other.permission_respond(turn, request, decision)['replayed']
    assert bridge.run(turn).wait(10)['state'] == 'completed'
    expected = {'allow': 'accept', 'deny': 'decline'}[decision]
    assert bridge.run(turn).text.startswith(expected + '|')
    assert other.permission_respond(turn, request, decision)['state'] == 'delivered'
    with pytest.raises(BridgeError) as error:
        other.permission_respond(turn, request, 'deny' if decision == 'allow' else 'allow')
    assert error.value.code == 'idempotency_conflict'


def test_stop_while_waiting_for_permission(setup_proxy):
    bridge, instance = setup_proxy()
    turn = bridge.message_create(instance, 'hello', permission_mode='default')['turn_id']
    request = pending(bridge, turn)
    assert bridge.turn_stop(turn, wait=True)['state'] == 'cancelled'
    with pytest.raises(BridgeError) as error:
        bridge.permission_respond(turn, request, 'allow')
    assert error.value.code == 'permission_expired'


def test_inline_inputs_reach_codex_and_replay(setup_proxy):
    bridge, instance = setup_proxy()
    attachments = [PICTURE, {'type': 'text', 'name': 'report.txt', 'text': 'attached content'}]
    message = bridge.message_create(instance, 'inspect', attachments=attachments, idempotency_key='input-1')
    run = bridge.run(message['turn_id'])
    assert run.wait(10)['state'] == 'completed'
    assert 'image=True' in run.text and 'attached content' in run.text
    assert not any(e['kind'] == 'permission.required' for e in bridge.turn_events(run.id))
    assert Bridge(bridge.root).message_create(instance, 'inspect', attachments=attachments,
                                            idempotency_key='input-1')['replayed']
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance, 'inspect', attachments=[], idempotency_key='input-1')
    assert error.value.code == 'idempotency_conflict'


@pytest.mark.parametrize('attachment', [
    {'type': 'image', 'media_type': 'image/png', 'data': 'bad'},
    {'type': 'image', 'media_type': 'image/png', 'data': base64.b64encode(b'not png').decode()},
    {'type': 'image', 'url': 'https://example.invalid/picture.png'},
    {'type': 'text', 'text': 'x', 'path': '/etc/passwd'},
    {'type': 'text', 'text': 'x' * (5 * 1024 * 1024 + 1)},
])
def test_invalid_attachments_fail_before_admission(setup_proxy, attachment):
    bridge, instance = setup_proxy()
    with pytest.raises(BridgeError):
        bridge.message_create(instance, 'inspect', attachments=[attachment])
    assert bridge.runs() == []


def test_old_options_still_replay_after_schema_extension(setup_proxy):
    bridge, instance = setup_proxy()
    options = RunOptions()
    turn, _ = bridge.store.admit('old-turn', instance, 'hello', options, 'old-key')
    with bridge.store.connect() as db:
        value = json.loads(bridge.run(turn).snapshot['options'])
        value.pop('attachments')
        db.execute('UPDATE runs SET options=? WHERE id=?', (json.dumps(value), turn))
    assert bridge.store.replay(instance, 'hello', options, 'old-key') == turn
    assert bridge.store.admit('new-turn', instance, 'hello', options, 'old-key') == (turn, False)
    bridge.store.finish(turn, 'cancelled', 'fixture_complete')


def test_models_come_from_proxy_observation_without_inference(setup_proxy):
    bridge, _ = setup_proxy()
    result = bridge.models(account_ref='test', refresh=True)
    assert result['source'] == 'agentbridge_routing'
    assert result['models'][0]['id'] == 'fixture-model'
    assert result['models'][0]['availability'] == 'proxy_observed'
    assert bridge.runs() == []


def test_attachment_evidence_redacts_credentials_and_reports_portable_omission(setup_proxy):
    bridge, instance = setup_proxy()
    inputs = [{'type': 'text', 'text': 'fixture-client-key'}, PICTURE]
    message = bridge.message_create(instance, 'fixture', attachments=inputs, idempotency_key='redacted-input')
    run = bridge.run(message['turn_id'])
    outcome = run.wait(10)
    assert outcome['state'] == 'completed', {
        'error': outcome['error'],
        'events': [(event.kind, event.data.get('code')) for event in run.events()],
    }
    assert 'fixture-client-key' not in json.dumps(run.snapshot)
    assert Bridge(bridge.root).message_create(instance, 'fixture', attachments=inputs,
                                             idempotency_key='redacted-input')['replayed']
    event = bridge.turn_events(run.id)[0]['data']
    assert event['attachment_content_omitted'] is True
    assert event['attachments'][0]['media_type'] == 'text/plain'
    assert bridge.messages(instance, role='user')[0]['attachments'] == event['attachments']


@pytest.mark.parametrize('value', ['', {}, 0, False, [{'type': []}], [{'type': 'text', 'text': '\ud800'}]])
def test_malformed_attachment_collection_rejected(setup_proxy, value):
    bridge, instance = setup_proxy()
    with pytest.raises(BridgeError):
        bridge.message_create(instance, 'fixture', attachments=value)
    assert not bridge.runs()


def test_stop_kills_codex_child_that_outlives_duplex_wrapper(setup_proxy):
    from agentbridge.process import alive, identity
    bridge, instance = setup_proxy(native_args=('--ignore-term',))
    turn = bridge.message_create(instance, 'fixture', permission_mode='default')['turn_id']
    pending(bridge, turn)
    data = next(e['data'] for e in bridge.turn_events(turn) if e['kind'] == 'permission.required')
    pid = int(data['input']['reason'])
    started = identity(pid)
    assert alive(pid, started)
    try:
        assert bridge.turn_stop(turn, wait=True)['state'] == 'cancelled'
        deadline = time.monotonic() + 3
        while alive(pid, started) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not alive(pid, started)
    finally:
        if alive(pid, started):
            import os
            import signal
            os.kill(pid, signal.SIGKILL)
