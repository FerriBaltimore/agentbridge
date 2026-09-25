import base64
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
import time

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.context_package import digest
from agentbridge.context_package import validate as validate_context
from agentbridge.context_package import materialize
from agentbridge.errors import BridgeError
from agentbridge.permissions import Permissions
from fixtures.test_proxy_account_fixture import proxy_account, seed_authenticated_proxy_account
from uuid import uuid4

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
        bridge = Bridge(tmp_path.parent / f'{tmp_path.name}-state')

        def create(*, native_args=(), evaluation=False, workspace_path=None,
                   native_command=None):
            account = replace(proxy_account('test', port, provider='codex'),
                              command=native_command or
                              ('/usr/bin/python3', '-c', FIXTURE.read_text(), *native_args))
            account = seed_authenticated_proxy_account(bridge.store, account,
                                                       observe_local=True)
            assert bridge.routes.observe(account)['status'] == 'active'
            instance = bridge.instance_create(account_ref='test', model='fixture-model',
                                              workspace_path=str(workspace_path or tmp_path),
                                              evaluation=evaluation)
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
    stopped = bridge.turn_stop(turn, wait=True)
    assert stopped['state'] == 'cancelled'
    assert stopped['turn_id'] == turn and stopped['instance_id'] == instance
    assert stopped['account_ref'] == 'test' and stopped['message_id']
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


def context_package(*, tools=()):
    rule = 'selected fixture rule'
    skill = '---\nname: fixture-skill\ndescription: Fixture skill.\n---\nUse selected inputs.\n'
    instructions = [
        {'instance_id': 'rule-1', 'revision': 1, 'kind': 'rule', 'scope': 'conversation',
         'assets': [{'path': 'RULE.md', 'content': rule, 'digest': sha256(rule.encode()).hexdigest()}]},
        {'instance_id': 'skill-1', 'revision': 1, 'kind': 'skill', 'scope': 'conversation',
         'assets': [{'path': 'SKILL.md', 'content': skill, 'digest': sha256(skill.encode()).hexdigest()}]},
    ]
    evidence_text = 'The fixture source says blue.'
    evidence = [{'source_ref': 'fixture:1', 'kind': 'excerpt', 'text': evidence_text,
                 'digest': sha256(evidence_text.encode()).hexdigest(),
                 'freshness': 'fresh', 'authority': 'record', 'channel': 'evidence'}]
    selection = {'context_refs': ['fixture:1'], 'instructions': [
        {**item, 'assets': [{'path': asset['path'], 'digest': asset['digest']}
                            for asset in item['assets']]} for item in instructions],
        'exclusions': [], 'tools': list(tools)}
    return {'version': 2, 'selection_hash': digest(selection), 'instructions': instructions,
            'exclusions': [], 'evidence': evidence, 'tools': list(tools)}


def test_context_package_and_mcp_reach_codex_without_persisting_capability(setup_proxy, tmp_path):
    bridge, instance = setup_proxy()
    package = context_package(tools=({'name': 'fixture-tool'},))
    capability = 'FIXTURE_PRIVATE_CAPABILITY_123456'
    mcp = {'version': 1, 'socket_path': str(tmp_path / 'fixture.sock'),
           'operation_id': str(uuid4()), 'capability': capability}
    accepted = bridge.message_create(instance, 'inspect-context', context_package=package,
                                     mcp=mcp, idempotency_key='context-1')
    run = bridge.run(accepted['turn_id'])
    assert run.wait(10)['state'] == 'completed'
    assert json.loads(run.text) == {'developer': True, 'evidence': True,
                                   'skill': True, 'mcp': True, 'shell_disabled': True,
                                   'skills_isolated': True, 'project_docs_disabled': True,
                                   'web_disabled': True, 'ephemeral': False}
    with pytest.raises(BridgeError) as error:
        run.resume()
    assert error.value.code == 'context_required'
    assert not list((bridge.root / 'codex-runtime' / instance / 'skills').glob('agentbridge-context-*'))
    saved = json.dumps([run.snapshot, bridge.turn_events(run.id)])
    assert capability not in saved and 'selected fixture rule' not in saved
    assert Bridge(bridge.root).message_create(instance, 'inspect-context', context_package=package,
                                              mcp=mcp, idempotency_key='context-1')['replayed']
    with pytest.raises(BridgeError) as error:
        changed = {**package, 'exclusions': ['new']}
        changed['selection_hash'] = digest({'context_refs': ['fixture:1'],
            'instructions': [{**item, 'assets': [{'path': asset['path'], 'digest': asset['digest']}
                for asset in item['assets']]} for item in package['instructions']],
            'exclusions': ['new'], 'tools': package['tools']})
        bridge.message_create(instance, 'inspect-context', context_package=changed,
                              mcp=mcp, idempotency_key='context-1')
    assert error.value.code == 'idempotency_conflict'


def test_context_without_mcp_disables_shell_and_unselected_skills(setup_proxy):
    bridge, instance = setup_proxy()
    turn = bridge.message_create(instance, 'inspect-context',
                                 context_package=context_package())['turn_id']
    run = bridge.run(turn)
    assert run.wait(10)['state'] == 'completed'
    result = json.loads(run.text)
    assert result['mcp'] is False
    assert result['shell_disabled'] is True
    assert result['skills_isolated'] is True


def test_missing_selected_skill_fails_before_codex_thread(setup_proxy):
    bridge, instance = setup_proxy(native_args=('--missing-skills',))
    turn = bridge.message_create(instance, 'inspect-context',
                                 context_package=context_package())['turn_id']
    run = bridge.run(turn)
    assert run.wait(10)['state'] == 'failed'
    assert run.snapshot['error'] == 'skill_unavailable'
    assert any(event.kind == 'error' and event.data['code'] == 'skill_unavailable'
               and event.data['outcome'] == 'not_started' for event in run.events())
    assert not any(event['kind'] == 'thread.started' for event in bridge.turn_events(turn))


def test_invalid_context_and_missing_mcp_fail_before_admission(setup_proxy):
    bridge, instance = setup_proxy()
    package = context_package()
    package['instructions'][0]['assets'][0]['content'] = 'modified'
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance, 'inspect-context', context_package=package)
    assert error.value.code == 'invalid_context'
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance, 'inspect-context',
                              context_package=context_package(tools=({'name': 'fixture-tool'},)))
    assert error.value.code == 'mcp_required'
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance, 'inspect-context', mcp={'version': 1,
            'socket_path': 'relative.sock', 'operation_id': str(uuid4()),
            'capability': 'FIXTURE_PRIVATE_CAPABILITY_123456'})
    assert error.value.code == 'invalid_mcp'
    assert bridge.runs() == []


def test_evaluation_context_requests_ephemeral_thread_and_disables_shell(setup_proxy, tmp_path):
    workspace = tmp_path / 'empty-evaluation-workspace'
    workspace.mkdir()
    bridge, instance = setup_proxy(evaluation=True, workspace_path=workspace,
        native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    package = context_package()
    package['execution_mode'] = 'evaluation_inputs_only'
    turn = bridge.message_create(instance, 'inspect-context', context_package=package)['turn_id']
    run = bridge.run(turn)
    assert run.wait(10)['state'] == 'completed'
    result = json.loads(run.text)
    assert result['ephemeral'] is True
    assert result['shell_disabled'] is True
    assert result['skills_isolated'] is True


def test_inputs_only_context_rejects_mcp_before_admission(setup_proxy, tmp_path):
    bridge, instance = setup_proxy()
    package = context_package()
    package['execution_mode'] = 'inputs_only'
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance, 'inspect-context', context_package=package,
            mcp={'version': 1, 'socket_path': str(tmp_path / 'tools.sock'),
                 'operation_id': str(uuid4()),
                 'capability': 'FIXTURE_PRIVATE_CAPABILITY_123456'})
    assert error.value.code == 'invalid_context'
    assert bridge.runs() == []


def test_legacy_context_package_without_tools_keeps_its_selection_hash():
    package = context_package()
    package['version'] = 1
    package.pop('tools')
    selection = {'context_refs': ['fixture:1'], 'instructions': [
        {**item, 'assets': [{'path': asset['path'], 'digest': asset['digest']}
                            for asset in item['assets']]} for item in package['instructions']],
        'exclusions': []}
    package['selection_hash'] = digest(selection)
    assert validate_context(package) is package


def test_context_materialization_clears_orphaned_skills(tmp_path):
    stale = tmp_path / 'skills' / 'agentbridge-context-old'
    stale.mkdir(parents=True)
    (stale / 'SKILL.md').write_text('stale')
    with materialize(tmp_path, context_package()) as (developer, skills, evidence):
        assert not stale.exists()
        assert 'selected fixture rule' in developer
        assert Path(skills[0]['path']).is_file()
        assert 'UNTRUSTED SOURCE EVIDENCE' in evidence[0]['text']
    assert not list((tmp_path / 'skills').glob('agentbridge-context-*'))


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
    from agentbridge.process import alive
    from fixtures.test_process_descendants import kill_owned, owned_descendant
    bridge, instance = setup_proxy(native_args=('--ignore-term',))
    turn = bridge.message_create(instance, 'fixture', permission_mode='default')['turn_id']
    pending(bridge, turn)
    data = next(e['data'] for e in bridge.turn_events(turn) if e['kind'] == 'permission.required')
    pid, started = owned_descendant(bridge.run(turn), int(data['input']['reason']))
    assert alive(pid, started)
    try:
        assert bridge.turn_stop(turn, wait=True)['state'] == 'cancelled'
        deadline = time.monotonic() + 3
        while alive(pid, started) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not alive(pid, started)
    finally:
        kill_owned(pid, started)
