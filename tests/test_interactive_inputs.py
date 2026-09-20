import base64
import json
from pathlib import Path
import sys
import time

import pytest

from agentbridge import Account, Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.permissions import Permissions

PICTURE = {'type': 'image', 'media_type': 'image/png',
           'data': base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()}
FIXTURE = Path(__file__).parent / 'fixtures' / 'test_duplex_provider.py'


@pytest.fixture(autouse=True)
def cleanup(tmp_path):
    yield
    if (tmp_path / 'state' / 'bridge.sqlite3').exists():
        bridge = Bridge(tmp_path / 'state')
        for row in bridge.runs():
            if row['state'] in {'starting', 'running', 'stopping'}:
                bridge.run(row['id']).stop(wait=True)


def setup(tmp_path, engine, env_names=(), native_args=()):
    home = tmp_path / 'native'
    home.mkdir()
    bridge = Bridge(tmp_path / 'state')
    bridge.register(Account('test', engine, home=str(home), command=(sys.executable, str(FIXTURE), *native_args), env_names=env_names))
    instance = bridge.instance_create(engine=engine, account_ref='test', workspace_path=str(tmp_path))
    return bridge, instance['id']


def pending(bridge, turn):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        events = [e for e in bridge.turn_events(turn) if e['kind'] == 'permission.required']
        if events:
            return events[0]['data']['permission_id']
        time.sleep(0.05)
    raise AssertionError(bridge.run(turn).snapshot)


@pytest.mark.parametrize('engine', ['codex', 'claude'])
@pytest.mark.parametrize('decision', ['allow', 'deny'])
def test_native_permission_roundtrip_after_client_restart(tmp_path, engine, decision):
    bridge, instance = setup(tmp_path, engine)
    turn = bridge.message_create(instance, 'hello', permission_mode='default')['turn_id']
    request = pending(bridge, turn)
    other = Bridge(bridge.root)
    with pytest.raises(BridgeError, match='matching'):
        Permissions(other.store).respond('wrong-turn', request, 'allow')
    result = other.permission_respond(turn, request, decision)
    assert result['state'] == 'queued'
    assert other.permission_respond(turn, request, decision)['replayed']
    assert bridge.run(turn).wait(10)['state'] == 'completed'
    expected = {'allow': 'accept', 'deny': 'decline'}[decision] if engine == 'codex' else decision
    assert bridge.run(turn).text.startswith(expected + '|')
    assert other.permission_respond(turn, request, decision)['state'] == 'delivered'
    with pytest.raises(BridgeError) as error:
        other.permission_respond(turn, request, 'deny' if decision == 'allow' else 'allow')
    assert error.value.code == 'idempotency_conflict'


@pytest.mark.parametrize('engine', ['codex', 'claude'])
def test_stop_while_waiting_for_permission(tmp_path, engine):
    bridge, instance = setup(tmp_path, engine)
    turn = bridge.message_create(instance, 'hello', permission_mode='default')['turn_id']
    request = pending(bridge, turn)
    assert bridge.turn_stop(turn, wait=True)['state'] == 'cancelled'
    with pytest.raises(BridgeError) as error:
        bridge.permission_respond(turn, request, 'allow')
    assert error.value.code == 'permission_expired'


@pytest.mark.parametrize('engine', ['codex', 'claude'])
def test_inline_inputs_reach_native_provider_and_replay(tmp_path, engine):
    bridge, instance = setup(tmp_path, engine)
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
def test_invalid_attachments_fail_before_admission(tmp_path, attachment):
    bridge, instance = setup(tmp_path, 'codex')
    with pytest.raises(BridgeError):
        bridge.message_create(instance, 'inspect', attachments=[attachment])
    assert bridge.runs() == []


def test_old_options_still_replay_after_schema_extension(tmp_path):
    bridge, instance = setup(tmp_path, 'codex')
    options = RunOptions()
    turn, _ = bridge.store.admit('old-turn', instance, 'hello', options, 'old-key')
    with bridge.store.connect() as db:
        value = json.loads(bridge.run(turn).snapshot['options'])
        value.pop('attachments')
        db.execute('UPDATE runs SET options=? WHERE id=?', (json.dumps(value), turn))
    assert bridge.store.replay(instance, 'hello', options, 'old-key') == turn
    assert bridge.store.admit('new-turn', instance, 'hello', options, 'old-key') == (turn, False)
    bridge.store.finish(turn, 'cancelled', 'fixture_complete')


def test_claude_catalog_uses_initialize_without_inference(tmp_path):
    bridge, _ = setup(tmp_path, 'claude')
    result = bridge.models('claude', account_ref='test', refresh=True)
    assert result['source'] == 'live'
    assert result['models'][0]['id'] == 'claude-fixture'
    assert bridge.runs() == []


def test_attachment_evidence_redacts_credentials_and_reports_portable_omission(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_ATTACHMENT_SECRET', 'fixture-secret-123')
    bridge, instance = setup(tmp_path, 'codex', env_names=('FIXTURE_ATTACHMENT_SECRET',))
    inputs = [{'type': 'text', 'text': 'fixture-secret-123'}]
    message = bridge.message_create(instance, 'fixture', attachments=inputs, idempotency_key='redacted-input')
    run = bridge.run(message['turn_id'])
    run.wait(10)
    assert 'fixture-secret-123' not in json.dumps(run.snapshot)
    assert Bridge(bridge.root).message_create(instance, 'fixture', attachments=inputs,
                                             idempotency_key='redacted-input')['replayed']
    event = bridge.turn_events(run.id)[0]['data']
    assert event['attachment_content_omitted'] is True
    assert event['attachments'][0]['media_type'] == 'text/plain'
    assert bridge.messages(instance, role='user')[0]['attachments'] == event['attachments']


@pytest.mark.parametrize('value', ['', {}, 0, False, [{'type': []}], [{'type': 'text', 'text': '\ud800'}]])
def test_malformed_attachment_collection_rejected(tmp_path, value):
    bridge, instance = setup(tmp_path, 'codex')
    with pytest.raises(BridgeError):
        bridge.message_create(instance, 'fixture', attachments=value)
    assert not bridge.runs()


@pytest.mark.parametrize('engine', ['codex', 'claude'])
def test_stop_kills_native_child_that_outlives_duplex_wrapper(tmp_path, engine):
    from agentbridge.process import alive, identity
    bridge, instance = setup(tmp_path, engine, native_args=('--ignore-term',))
    turn = bridge.message_create(instance, 'fixture', permission_mode='default')['turn_id']
    pending(bridge, turn)
    data = next(e['data'] for e in bridge.turn_events(turn) if e['kind'] == 'permission.required')
    pid = int(data['input']['reason']) if engine == 'codex' else data['input']['fixture_pid']
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
