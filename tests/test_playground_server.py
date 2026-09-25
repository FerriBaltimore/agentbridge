"""The local HTTP playground reaches account and turn operations through Bridge."""

import http.client
import json
from threading import Thread

import pytest

from agentbridge import BridgeError
from playground.server import create_server


class PublicBridgeStub:
    """Only public SDK methods: accidental Store access fails this fixture."""

    def __init__(self):
        self.calls = []
        self.fail_models = False

    def _record(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))

    def capabilities(self):
        return {'execution': {'engine': 'codex'}}

    def accounts(self):
        return [{'id': 'account-id', 'name': 'Personal', 'provider': 'codex',
                 'email': 'user@example.invalid', 'supported_models': ['fixture-model'],
                 'key_env': 'PRIVATE_ENV_REFERENCE', 'credential_ref': 'private'}]

    def account_reference(self, account_id):
        assert account_id == 'account-id'
        return 'Personal'

    def account_status(self, *, account_ref, refresh):
        self._record('account_status', account_ref=account_ref, refresh=refresh)
        return {'account_id': 'account-id', 'authentication': {'status': 'active'}}

    def account_usage(self, *, account_ref, refresh):
        self._record('account_usage', account_ref=account_ref, refresh=refresh)
        return {'supported': False, 'stale': True, 'reason': 'not_observed'}

    def account_reset_credits(self, account_ref, *, refresh):
        self._record('account_reset_credits', account_ref, refresh=refresh)
        return {'account_id': 'account-id', 'account_ref': account_ref,
                'provider': 'codex', 'status': 'available', 'available_count': 1,
                'credits': [{'id': 'fixture-credit', 'status': 'available'}],
                'observed_at': '2026-09-25T00:00:00Z', 'stale': False,
                'observation_ref': 'fixture-observation'}

    def account_quota_reset(self, account_ref, *, idempotency_key,
                            observation_ref, credit_id=None):
        self._record('account_quota_reset', account_ref,
                     idempotency_key=idempotency_key,
                     observation_ref=observation_ref, credit_id=credit_id)
        return {'outcome': 'reset', 'reset_credits': {
            **self.account_reset_credits(account_ref, refresh=False),
            'status': 'none', 'available_count': 0, 'credits': []}}

    def account_login_start(self, **options):
        self._record('account_login_start', **options)
        return {'attempt_id': 'login-1', 'owner_ref': 'owner-1', 'status': 'awaiting_user',
                'authorization_url': 'https://auth.example.test/authorize'}

    def account_login_status(self, attempt_id, *, owner_ref):
        self._record('account_login_status', attempt_id, owner_ref=owner_ref)
        return {'attempt_id': attempt_id, 'status': 'authorized'}

    def account_login_check(self, attempt_id, *, owner_ref):
        self._record('account_login_check', attempt_id, owner_ref=owner_ref)
        return {'attempt_id': attempt_id, 'status': 'verified'}

    def account_login_complete(self, attempt_id, *, owner_ref):
        self._record('account_login_complete', attempt_id, owner_ref=owner_ref)
        return {'attempt': {'attempt_id': attempt_id, 'status': 'bound'},
                'account': {'id': 'account-id', 'name': 'Personal',
                            'provider': 'codex', 'key_env': 'PRIVATE_ENV_REFERENCE'}}

    def account_login_cancel(self, attempt_id, *, owner_ref):
        self._record('account_login_cancel', attempt_id, owner_ref=owner_ref)
        return {'attempt_id': attempt_id, 'status': 'cancelled'}

    def account_delete(self, account_ref):
        self._record('account_delete', account_ref)
        return {'account_ref': account_ref, 'removed': True,
                'upstream_credential_removed': False}

    def models(self, *, refresh=False):
        self._record('models', refresh=refresh)
        if self.fail_models:
            raise BridgeError('model_unavailable', 'No model is available.')
        return {'items': [{'id': 'fixture-model'}], 'stale': False}

    def instances(self, *, limit):
        self._record('instances', limit=limit)
        return [{'instance_id': 'instance-1'}]

    def instance_create(self, **options):
        self._record('instance_create', **options)
        return {'instance_id': 'instance-1', 'model': options['model']}

    def instance_delete(self, instance_id):
        self._record('instance_delete', instance_id)
        return {'instance_id': instance_id, 'deleted': True, 'pending': False}

    def instance_get(self, instance_id, *, include_last_turn):
        self._record('instance_get', instance_id, include_last_turn=include_last_turn)
        return {'instance_id': instance_id,
                'last_turn': {'id': 'turn-1', 'state': 'running',
                              'prompt': 'private-fixture-prompt'}}

    def instance_update(self, instance_id, **options):
        self._record('instance_update', instance_id, **options)
        return {'instance_id': instance_id, 'model': options.get('model', 'fixture-model'),
                'routing_provider': options.get('provider'),
                'version': options['expected_version'] + 1}

    def messages(self, instance_id, *, limit):
        self._record('messages', instance_id, limit=limit)
        return [{'role': 'assistant', 'content': 'Hello'}]

    def instance_events(self, instance_id, *, after_seq, limit):
        self._record('instance_events', instance_id, after_seq=after_seq, limit=limit)
        return [{'seq': after_seq + 1, 'type': 'turn.started'}]

    def message_create(self, instance_id, **options):
        self._record('message_create', instance_id, **options)
        return {'turn_id': 'turn-1', 'state': 'running'}

    def turn(self, turn_id, *, include_usage, include_error):
        self._record('turn', turn_id, include_usage=include_usage,
                     include_error=include_error)
        return {'turn_id': turn_id, 'state': 'completed'}

    def turn_events(self, turn_id, *, after_seq, limit):
        self._record('turn_events', turn_id, after_seq=after_seq, limit=limit)
        return [{'seq': after_seq + 1, 'type': 'turn.completed'}]

    def turn_stop(self, turn_id, **options):
        self._record('turn_stop', turn_id, **options)
        return {'turn_id': turn_id, 'state': 'cancelled'}

    def permission_respond(self, turn_id, permission_id, **options):
        self._record('permission_respond', turn_id, permission_id, **options)
        return {'permission_id': permission_id, 'decision': options['decision']}


class FakeAuthBrowser:
    def __init__(self):
        self.launched = []
        self.stopped = []
        self.closed = False

    def launch(self, attempt):
        self.launched.append(attempt)
        return True

    def stop(self, attempt_id):
        self.stopped.append(attempt_id)

    def active(self, attempt_id):
        return (any(attempt['attempt_id'] == attempt_id for attempt in self.launched)
                and attempt_id not in self.stopped)

    def close(self):
        self.closed = True


@pytest.fixture
def local_server(tmp_path):
    static = tmp_path / 'static'
    static.mkdir()
    (static / 'index.html').write_text('<h1>Playground</h1>')
    bridge = PublicBridgeStub()
    server = create_server(port=0, bridge=bridge, static_root=static,
                           workspace_path=tmp_path)
    worker = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01},
                    daemon=True)
    worker.start()
    try:
        yield server, bridge
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()


@pytest.fixture
def browser_server(tmp_path):
    bridge = PublicBridgeStub()
    auth_browser = FakeAuthBrowser()
    server = create_server(port=0, bridge=bridge, workspace_path=tmp_path,
                           auth_browser=auth_browser)
    worker = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01},
                    daemon=True)
    worker.start()
    try:
        yield server, bridge, auth_browser
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()
        assert auth_browser.closed


def request(server, method, path, *, body=None, headers=None, csrf=True):
    headers = dict(headers or {})
    payload = None if body is None else json.dumps(body).encode()
    if method == 'POST':
        headers.setdefault('Content-Type', 'application/json')
    if method != 'GET' and csrf:
        headers.setdefault('X-AgentBridge-Playground', '1')
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        content = response.read()
        return response.status, (json.loads(content) if
                                 response.getheader('Content-Type', '').startswith('application/json')
                                 else content.decode())
    finally:
        connection.close()


def test_default_playground_uses_private_state_and_serves_sdk_routes(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state-home'))
    server = create_server(port=0)
    worker = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01},
                    daemon=True)
    worker.start()
    try:
        assert server.bridge.root == tmp_path / 'state-home/agentbridge'
        assert request(server, 'GET', '/api/meta') == (
            200, {'result': {'workspace_path': str(workspace)}})
        assert request(server, 'GET', '/api/accounts') == (200, {'result': []})
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()
        server.bridge.close()


def test_read_routes_expose_safe_account_projection_and_polling(local_server, tmp_path):
    server, bridge = local_server
    assert request(server, 'GET', '/')[1] == '<h1>Playground</h1>'
    assert request(server, 'GET', '/api/meta')[1] == {
        'result': {'workspace_path': str(tmp_path)}}
    assert request(server, 'GET', '/api/capabilities')[1]['result']['execution']['engine'] == 'codex'
    account = request(server, 'GET', '/api/accounts')[1]['result'][0]
    assert account == {'account_ref': 'Personal', 'name': 'Personal',
                       'email': 'user@example.invalid', 'provider': 'codex',
                       'supported_models': ['fixture-model']}
    assert request(server, 'GET', '/api/accounts/Personal/status?refresh=1')[1]['result'][
        'authentication']['status'] == 'active'
    assert request(server, 'GET', '/api/accounts/Personal/usage')[1]['result']['stale']
    assert request(server, 'GET', '/api/models?refresh=1')[1]['result']['items'][0]['id'] == 'fixture-model'
    assert request(server, 'GET', '/api/instances')[1]['result'][0]['instance_id'] == 'instance-1'
    instance = request(server, 'GET', '/api/instances/instance-1')[1]['result']
    assert instance['instance_id'] == 'instance-1'
    assert instance['last_turn'] == {'turn_id': 'turn-1', 'state': 'running'}
    assert 'private-fixture-prompt' not in repr(instance)
    assert request(server, 'GET', '/api/instances/instance-1/messages')[1]['result'][0][
        'content'] == 'Hello'
    assert request(server, 'GET', '/api/instances/instance-1/events?after_seq=7')[1][
        'result'][0]['seq'] == 8
    assert request(server, 'GET', '/api/turns/turn-1')[1]['result']['state'] == 'completed'
    assert request(server, 'GET', '/api/turns/turn-1/events?after_seq=9')[1][
        'result'][0]['seq'] == 10
    assert ('account_status', (), {'account_ref': 'Personal', 'refresh': True}) in bridge.calls
    assert ('models', (), {'refresh': True}) in bridge.calls


def test_reset_routes_use_only_public_sdk_and_require_explicit_mutation(local_server):
    server, bridge = local_server
    credits = request(server, 'GET', '/api/accounts/Personal/reset-credits?refresh=1')
    assert credits[0] == 200 and credits[1]['result']['available_count'] == 1
    assert ('account_reset_credits', ('Personal',), {'refresh': True}) in bridge.calls
    operation = {'idempotency_key': 'fixture-request',
                 'observation_ref': 'fixture-observation', 'credit_id': 'fixture-credit'}
    status, denied = request(server, 'POST', '/api/accounts/Personal/quota/reset',
                             body=operation, csrf=False)
    assert status == 403 and denied['error']['code'] == 'forbidden'
    status, invalid = request(server, 'POST', '/api/accounts/Personal/quota/reset',
                              body={**operation, 'extra': 'ignored'})
    assert status == 400 and invalid['error']['code'] == 'invalid_request'
    status, result = request(server, 'POST', '/api/accounts/Personal/quota/reset',
                             body=operation)
    assert status == 200 and result['result']['outcome'] == 'reset'
    assert ('account_quota_reset', ('Personal',), operation) in bridge.calls


def test_account_projection_uses_sdk_reference_after_name_collision(local_server,
                                                                    monkeypatch):
    server, bridge = local_server
    monkeypatch.setattr(bridge, 'account_reference',
                        lambda account_id: f'id:{account_id}')
    account = request(server, 'GET', '/api/accounts')[1]['result'][0]
    assert account['account_ref'] == 'id:account-id'
    assert account['name'] == 'Personal'
    completed = request(server, 'POST', '/api/accounts/login/login-1/complete',
                        body={'owner_ref': 'owner-1'})[1]['result']
    assert completed['account']['account_ref'] == 'id:account-id'
    assert 'key_env' not in repr(account) + repr(completed['account'])


def test_login_and_removal_only_call_public_sdk(local_server):
    server, bridge = local_server
    start = {'provider': 'codex', 'name': 'Personal'}
    assert request(server, 'POST', '/api/accounts/login/start', body=start)[1][
        'result']['owner_ref'] == 'owner-1'
    status, rejected = request(server, 'POST', '/api/accounts/login/start',
                               body={**start, 'key_env': 'UNEXPECTED_KEY'})
    assert status == 400 and rejected['error']['code'] == 'invalid_request'
    assert request(server, 'GET', '/api/accounts/login/login-1?owner_ref=owner-1')[1][
        'result']['status'] == 'authorized'
    assert request(server, 'POST', '/api/accounts/login/login-1/check',
                   body={'owner_ref': 'owner-1'})[1]['result']['status'] == 'verified'
    completed = request(server, 'POST', '/api/accounts/login/login-1/complete',
                        body={'owner_ref': 'owner-1'})[1]['result']
    assert completed['account']['account_ref'] == 'Personal'
    assert 'key_env' not in completed['account']
    assert request(server, 'POST', '/api/accounts/login/login-1/cancel',
                   body={'owner_ref': 'owner-1'})[1]['result']['status'] == 'cancelled'
    removed = request(server, 'DELETE', '/api/accounts/Personal')[1]['result']
    assert removed == {'account_ref': 'Personal', 'removed': True,
                       'upstream_credential_removed': False}
    assert ('account_login_start', (), start) in bridge.calls
    assert ('account_delete', ('Personal',), {}) in bridge.calls


def test_delete_conversation_uses_public_sdk_and_mutation_guard(local_server):
    server, bridge = local_server
    status, rejected = request(server, 'DELETE', '/api/instances/instance-1', csrf=False)
    assert status == 403 and rejected['error']['code'] == 'forbidden'
    assert not any(call[0] == 'instance_delete' for call in bridge.calls)
    status, payload = request(server, 'DELETE', '/api/instances/instance-1')
    assert status == 200
    assert payload['result'] == {'instance_id': 'instance-1', 'deleted': True,
                                 'pending': False}
    assert ('instance_delete', ('instance-1',), {}) in bridge.calls


def test_login_start_opens_isolated_browser_once_and_forwards_email(browser_server):
    server, bridge, auth_browser = browser_server
    values = {'provider': 'claude', 'name': 'Work', 'email': 'work@example.test'}
    status, payload = request(server, 'POST', '/api/accounts/login/start', body=values)
    assert status == 200
    assert payload['result']['browser_opened'] is True
    assert auth_browser.launched == [{
        'attempt_id': 'login-1', 'owner_ref': 'owner-1', 'status': 'awaiting_user',
        'authorization_url': 'https://auth.example.test/authorize'}]
    assert ('account_login_start', (), values) in bridge.calls
    status_result = request(server, 'GET', '/api/accounts/login/login-1?owner_ref=owner-1')
    assert status_result[0] == 200
    assert status_result[1]['result']['browser_opened'] is False
    assert len(auth_browser.launched) == 1


def test_isolated_browser_stops_after_terminal_status(browser_server, monkeypatch):
    server, bridge, auth_browser = browser_server
    request(server, 'POST', '/api/accounts/login/start',
            body={'provider': 'claude', 'name': 'Work'})
    pending = lambda *_args, **_kwargs: {'status': 'awaiting_user'}
    monkeypatch.setattr(bridge, 'account_login_status', pending)
    pending_result = request(server, 'GET', '/api/accounts/login/login-1?owner_ref=owner-1')
    assert pending_result[1]['result']['browser_opened'] is True
    assert auth_browser.stopped == []
    authorized = lambda *_args, **_kwargs: {'status': 'authorized'}
    monkeypatch.setattr(bridge, 'account_login_status', authorized)
    authorized_result = request(server, 'GET', '/api/accounts/login/login-1?owner_ref=owner-1')
    assert authorized_result[1]['result']['browser_opened'] is False
    assert auth_browser.stopped == ['login-1']


def test_isolated_browser_stops_after_cancel(browser_server):
    server, _, auth_browser = browser_server
    request(server, 'POST', '/api/accounts/login/start',
            body={'provider': 'claude', 'name': 'Work'})
    request(server, 'POST', '/api/accounts/login/login-1/cancel',
            body={'owner_ref': 'owner-1'})
    assert auth_browser.stopped == ['login-1']
    assert len(auth_browser.launched) == 1


def test_chat_routes_forward_optional_turn_controls(local_server, tmp_path):
    server, bridge = local_server
    assert request(server, 'POST', '/api/instances',
                   body={'model': 'fixture-model', 'provider': 'codex',
                         'idempotency_key': 'create-1'})[1][
                       'result']['instance_id'] == 'instance-1'
    update = {'expected_version': 2, 'provider': 'claude', 'model': 'fixture-claude'}
    assert request(server, 'POST', '/api/instances/instance-1', body=update)[1][
        'result']['routing_provider'] == 'claude'
    assert ('instance_update', ('instance-1',), update) in bridge.calls
    status, rejected = request(server, 'POST', '/api/instances/instance-1',
                               body={**update, 'secret': 'must-not-pass'})
    assert status == 400 and rejected['error']['code'] == 'invalid_request'
    message = {'content': 'Hello', 'model': 'fixture-model', 'effort': 'high',
               'context_window': 200000, 'permission_mode': 'dontAsk',
               'sandbox_mode': 'read-only', 'timeout_ms': 1000,
               'idempotency_key': 'message-1'}
    assert request(server, 'POST', '/api/instances/instance-1/messages',
                   body=message)[1]['result']['turn_id'] == 'turn-1'
    assert request(server, 'POST', '/api/turns/turn-1/permissions/permission-1',
                   body={'decision': 'accept'})[1]['result']['decision'] == 'accept'
    assert request(server, 'POST', '/api/turns/turn-1/stop',
                   body={'wait': False})[1]['result']['state'] == 'cancelled'
    assert ('instance_create', (), {'model': 'fixture-model', 'provider': 'codex',
                                    'workspace_path': str(tmp_path),
                                    'idempotency_key': 'create-1'}) in bridge.calls
    assert ('message_create', ('instance-1',), message) in bridge.calls


def test_host_csrf_and_errors_are_bounded(local_server):
    server, bridge = local_server
    status, result = request(server, 'GET', '/api/accounts',
                             headers={'Host': 'attacker.invalid'})
    assert status == 403 and result['error']['code'] == 'forbidden'
    status, result = request(server, 'GET', '/api/accounts',
                             headers={'Origin': 'http://attacker.invalid'})
    assert status == 403 and result['error']['code'] == 'forbidden'
    status, result = request(server, 'POST', '/api/instances',
                             body={'model': 'fixture-model'}, csrf=False)
    assert status == 403 and result['error']['code'] == 'forbidden'
    status, result = request(server, 'POST', '/api/instances', body={'model': 'fixture-model',
                             'secret': 'must-not-pass'})
    assert status == 400 and result['error']['code'] == 'invalid_request'
    status, result = request(server, 'GET', '/api/instances/instance-1/events?after_seq=-1')
    assert status == 400 and result['error']['code'] == 'invalid_request'
    bridge.fail_models = True
    status, result = request(server, 'GET', '/api/models')
    assert status == 400 and result['error']['code'] == 'model_unavailable'
    assert request(server, 'GET', '/static/../../../../etc/passwd')[0] == 404
