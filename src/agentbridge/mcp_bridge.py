"""stdio MCP server forwarding one admitted execution to a private Unix socket."""
import json
import os
import socket
import sys
from uuid import uuid4

from .errors import BridgeError
from .execution_context import (MCP_CAPABILITY_ENV, MCP_OPERATION_ENV, MCP_SOCKET_ENV,
                                validate_mcp)


MAX_REQUEST = 131_072
MAX_RESPONSE = 524_288
CHANNEL_ID = str(uuid4())


def reply(request_id, *, result=None, code=None, message=None):
    value = {'jsonrpc': '2.0', 'id': request_id}
    if code is None:
        value['result'] = result
    else:
        value['error'] = {'code': code, 'message': message}
    return value


def descriptor():
    value = {'version': 1, 'socket_path': os.environ[MCP_SOCKET_ENV],
             'operation_id': os.environ[MCP_OPERATION_ENV],
             'capability': os.environ[MCP_CAPABILITY_ENV]}
    return validate_mcp(value)


def forward(bound, method, params):
    frame = {'version': 1, 'operation_id': bound['operation_id'],
             'capability': bound['capability'], 'channel_id': CHANNEL_ID,
             'method': method, 'params': params}
    encoded = (json.dumps(frame, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')) + '\n').encode()
    if len(encoded) > MAX_REQUEST:
        raise ValueError('request_too_large')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(20)
        connection.connect(bound['socket_path'])
        connection.sendall(encoded)
        with connection.makefile('rb') as stream:
            raw = stream.readline(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE or not raw.endswith(b'\n'):
        raise ValueError('invalid_response')
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) not in ({'result'}, {'error'}):
        raise ValueError('invalid_response')
    return value


def dispatch(bound, request):
    if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
        return reply(None, code=-32600, message='Invalid request.')
    method, request_id = request.get('method'), request.get('id')
    if isinstance(method, str) and method.startswith('notifications/') and 'id' not in request:
        return None
    if 'id' not in request or not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        return reply(None, code=-32600, message='Invalid request.')
    params = request.get('params') or {}
    if not isinstance(params, dict):
        return reply(request_id, code=-32602, message='Invalid parameters.')
    if method == 'initialize':
        version = params.get('protocolVersion', '2024-11-05')
        if version not in ('2024-11-05', '2025-03-26', '2025-06-18'):
            version = '2025-06-18'
        return reply(request_id, result={'protocolVersion': version,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'agentbridge-execution', 'version': '2.0.0'}})
    if method == 'ping':
        return reply(request_id, result={})
    if method == 'tools/list':
        if (set(params) - {'cursor', '_meta'}
                or params.get('cursor') is not None
                or ('_meta' in params and not isinstance(params['_meta'], dict))):
            return reply(request_id, code=-32602, message='Invalid parameters.')
        query = {}
    elif method == 'tools/call':
        name, arguments = params.get('name'), params.get('arguments', {})
        if arguments is None:
            arguments = {}
        if (not isinstance(name, str) or not 1 <= len(name) <= 200
                or not isinstance(arguments, dict) or set(params) - {'name', 'arguments', '_meta'}
                or ('_meta' in params and not isinstance(params['_meta'], dict))):
            return reply(request_id, code=-32602, message='Invalid parameters.')
        query = {'call_id': str(uuid4()), 'name': name, 'arguments': arguments}
    else:
        return reply(request_id, code=-32601, message='Method not found.')
    try:
        value = forward(bound, method, query)
        if 'error' in value:
            return reply(request_id, code=-32000, message='Execution tool unavailable.')
        result = value['result']
        if not isinstance(result, dict):
            raise ValueError('invalid_response')
        if method == 'tools/list' and not isinstance(result.get('tools'), list):
            raise ValueError('invalid_response')
        if method == 'tools/call' and not isinstance(result.get('content'), list):
            raise ValueError('invalid_response')
        return reply(request_id, result=result)
    except (OSError, ValueError, TypeError, UnicodeError):
        return reply(request_id, code=-32000, message='Execution tool unavailable.')


def main():
    try:
        bound = descriptor()
    except (BridgeError, KeyError, ValueError, TypeError):
        return 1
    while True:
        raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        if not raw:
            return 0
        if len(raw) > MAX_REQUEST:
            if not raw.endswith(b'\n'):
                while raw and not raw.endswith(b'\n'):
                    raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
            response = reply(None, code=-32600, message='Request too large.')
        else:
            try:
                request = json.loads(raw)
                response = dispatch(bound, request)
            except (ValueError, UnicodeError):
                response = reply(None, code=-32700, message='Invalid JSON.')
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    raise SystemExit(main())
