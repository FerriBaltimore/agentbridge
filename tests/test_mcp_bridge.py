"""Fixture-only stdio MCP to private Unix facade boundary."""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
from threading import Thread
from uuid import UUID, uuid4

from agentbridge.execution_context import (MCP_CAPABILITY_ENV, MCP_OPERATION_ENV,
                                           MCP_SOCKET_ENV)


def test_stdio_mcp_forwards_admitted_tools_without_exposing_capability(tmp_path):
    path = tmp_path / 'tools.sock'
    operation_id = str(uuid4())
    capability = 'FIXTURE_PRIVATE_CAPABILITY_123456'
    observed = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            value = json.loads(self.rfile.readline())
            observed.append(value)
            assert set(value) == {'version', 'operation_id', 'capability',
                                  'channel_id', 'method', 'params'}
            assert value['operation_id'] == operation_id
            assert value['capability'] == capability
            if value['method'] == 'tools/list':
                response = {'result': {'tools': [{'name': 'fixture_echo',
                    'description': 'Fixture echo', 'inputSchema': {'type': 'object'}}]}}
            elif value['params']['name'] == 'denied':
                response = {'error': {'code': 'mcp_authorization_rejected',
                                      'message': 'PRIVATE FACADE BODY'}}
            else:
                response = {'result': {'content': [{'type': 'text', 'text': 'fixture result'}],
                                       'isError': False}}
            self.wfile.write((json.dumps(response) + '\n').encode())

    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list',
             'params': {'cursor': None, '_meta': {'progressToken': 2}}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'fixture_echo', 'arguments': {'value': 1},
                        '_meta': {'progressToken': 3}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
             'params': {'name': 'denied', 'arguments': None}},
        ]
        env = {key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ}
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        env.update({MCP_SOCKET_ENV: str(path), MCP_OPERATION_ENV: operation_id,
                    MCP_CAPABILITY_ENV: capability})
        process = subprocess.run([sys.executable, '-P', '-m', 'agentbridge.mcp_bridge'],
            input=''.join(json.dumps(value) + '\n' for value in requests),
            text=True, capture_output=True, env=env, timeout=5, check=True)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert [value['id'] for value in responses] == [1, 2, 3, 4]
    assert responses[0]['result']['capabilities']['tools'] == {'listChanged': False}
    assert responses[1]['result']['tools'][0]['name'] == 'fixture_echo'
    assert responses[2]['result']['content'][0]['text'] == 'fixture result'
    assert responses[3]['error']['code'] == -32000
    assert 'PRIVATE FACADE BODY' not in process.stdout
    assert capability not in process.stdout + process.stderr
    assert [value['method'] for value in observed] == ['tools/list', 'tools/call', 'tools/call']
    assert observed[0]['channel_id'] == observed[1]['channel_id'] == observed[2]['channel_id']
    assert str(UUID(observed[1]['params']['call_id'])) == observed[1]['params']['call_id']
    assert observed[2]['params']['arguments'] == {}
