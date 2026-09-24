"""Exercise SDK-backed turn SSE replay without a live provider account."""

from contextlib import contextmanager
import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agentbridge.errors import BridgeError
from playground.server import create_server
from test_interface import bridge_for_interface


@contextmanager
def _server(bridge, workspace):
    server = create_server(bridge.root, port=0, bridge=bridge, workspace_path=workspace)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _frames(body):
    result = []
    for chunk in body.split('\n\n'):
        fields = {}
        for line in chunk.splitlines():
            if ': ' in line:
                name, value = line.split(': ', 1)
                fields[name] = value
        if 'data' in fields:
            result.append(fields)
    return result


def test_sse_replays_normalized_events_by_sequence(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='Test',
                                      workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hi')
    bridge.run(accepted['turn_id']).wait(10)
    with _server(bridge, tmp_path) as base:
        url = f'{base}/api/turns/{accepted["turn_id"]}/stream?after_seq=0'
        with urlopen(url, timeout=10) as response:
            assert response.headers.get_content_type() == 'text/event-stream'
            frames = _frames(response.read().decode('utf-8'))
        rows = [json.loads(frame['data']) for frame in frames if 'id' in frame]
        assert rows
        assert [row['seq'] for row in rows] == sorted(row['seq'] for row in rows)
        assert [int(frame['id']) for frame in frames if 'id' in frame] == [
            row['seq'] for row in rows]
        assert any(row['kind'] == 'message.completed' for row in rows)
        assert rows[-1]['kind'] == 'run.finished'
        assert rows[-1]['data']['state'] == 'completed'
        assert frames[-1]['event'] == 'end'

        cursor = rows[len(rows) // 2]['seq']
        request = Request(url, headers={'Last-Event-ID': str(cursor)})
        with urlopen(request, timeout=10) as response:
            resumed = _frames(response.read().decode('utf-8'))
        assert [int(frame['id']) for frame in resumed if 'id' in frame] == [
            row['seq'] for row in rows if row['seq'] > cursor]
        assert resumed[-1]['event'] == 'end'


def test_sse_rejects_invalid_resume_cursor(tmp_path, bridge_for_interface):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='Test',
                                      workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hi')
    bridge.run(accepted['turn_id']).wait(10)
    with _server(bridge, tmp_path) as base:
        url = f'{base}/api/turns/{accepted["turn_id"]}/stream'
        request = Request(url, headers={'Last-Event-ID': 'invalid'})
        try:
            urlopen(request, timeout=10)
        except HTTPError as error:
            assert error.code == 400
            assert json.loads(error.read())['error']['code'] == 'invalid_request'
        else:
            raise AssertionError('Invalid Last-Event-ID was accepted.')


def test_sse_transport_failure_sends_only_safe_code(tmp_path, bridge_for_interface, monkeypatch):
    bridge = bridge_for_interface
    instance = bridge.instance_create(model='fixture-model', account_ref='Test',
                                      workspace_path=str(tmp_path))
    accepted = bridge.message_create(instance['id'], 'hi')
    bridge.run(accepted['turn_id']).wait(10)

    def fail_stream(*_args, **_kwargs):
        raise BridgeError('provider_protocol_error', 'PRIVATE PROVIDER BODY')

    monkeypatch.setattr(bridge, 'turn_events_stream', fail_stream)
    with _server(bridge, tmp_path) as base:
        with urlopen(f'{base}/api/turns/{accepted["turn_id"]}/stream', timeout=10) as response:
            body = response.read().decode('utf-8')
        frames = _frames(body)
        assert frames[-1]['event'] == 'stream.error'
        assert json.loads(frames[-1]['data']) == {'code': 'provider_protocol_error'}
        assert 'PRIVATE PROVIDER BODY' not in body
