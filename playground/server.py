"""Loopback HTTP playground backed exclusively by the public Bridge SDK."""

import argparse
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from agentbridge import Bridge, BridgeError


MAX_BODY_BYTES = 64 * 1024
MAX_PATH_BYTES = 4096
STATIC_ROOT = Path(__file__).parent / 'static'


def _account(value):
    data = asdict(value) if is_dataclass(value) else dict(value)
    return {'account_ref': data.get('name') or data.get('id'),
            'name': data.get('name'), 'email': data.get('email'),
            'provider': data.get('provider'),
            'supported_models': list(data.get('supported_models') or ())}


def _query_flag(query, name):
    value = query.get(name, ['0'])[-1]
    if value in ('1', 'true'):
        return True
    if value in ('0', 'false'):
        return False
    raise BridgeError('invalid_request', f'{name} must be true or false.')


def _query_number(query, name, default):
    value = query.get(name, [str(default)])[-1]
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BridgeError('invalid_request', f'{name} must be a nonnegative integer.') from None
    if not 0 <= number <= 1_000_000_000:
        raise BridgeError('invalid_request', f'{name} must be a nonnegative integer.')
    return number


def _fields(body, required, optional=()):
    if not isinstance(body, dict) or not set(required) <= body.keys() or not body.keys() <= set(required) | set(optional):
        raise BridgeError('invalid_request', 'Request fields do not match this operation.')
    return body


class PlaygroundServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, bridge, *, static_root=STATIC_ROOT, workspace_path=None):
        self.bridge = bridge
        self.static_root = Path(static_root).resolve()
        self.workspace_path = str(Path(workspace_path or '.').resolve())
        super().__init__(address, PlaygroundHandler)


class PlaygroundHandler(BaseHTTPRequestHandler):
    server: PlaygroundServer

    def log_message(self, *_):
        # URLs may contain opaque owner references; never emit request data.
        return

    def handle_get(self):
        self._handle('GET')

    def handle_post(self):
        self._handle('POST')

    def handle_delete(self):
        self._handle('DELETE')

    def handle_options(self):
        self._json(405, {'error': {'code': 'method_not_allowed',
                                  'message': 'Cross-origin requests are unavailable.'}})

    def _handle(self, method):
        try:
            self._same_host()
            if len(self.path.encode('utf-8')) > MAX_PATH_BYTES:
                raise BridgeError('invalid_request', 'Request path is too long.')
            parsed = urlsplit(self.path)
            if not parsed.path.startswith('/api/'):
                if method != 'GET':
                    raise BridgeError('method_not_allowed', 'This path is read-only.')
                return self._static(parsed.path)
            if method != 'GET':
                self._mutation_guard()
            segments = [unquote(item) for item in parsed.path.split('/') if item]
            query = parse_qs(parsed.query, keep_blank_values=True)
            body = self._body() if method == 'POST' else None
            result = self._dispatch(method, segments, query, body)
            self._json(200, {'result': result})
        except BridgeError as error:
            status = (403 if error.code == 'forbidden' else
                      405 if error.code == 'method_not_allowed' else
                      404 if error.code in {'not_found', 'account_not_found', 'instance_not_found',
                                            'authentication_attempt_not_found'} else
                      409 if error.code in {'busy', 'authentication_in_progress',
                                            'idempotency_conflict'} else 400)
            self._json(status, {'error': {'code': error.code, 'message': str(error),
                                          'data': error.safe_data()}})
        except (TypeError, ValueError, KeyError):
            self._json(400, {'error': {'code': 'invalid_request', 'message': 'Invalid request.'}})
        except Exception:
            self._json(500, {'error': {'code': 'internal_error', 'message': 'Internal error.'}})

    def _same_host(self):
        port = self.server.server_port
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if host not in allowed or (origin and origin != f'http://{host}'):
            raise BridgeError('forbidden', 'Use the local playground address.')

    def _mutation_guard(self):
        if self.headers.get('X-AgentBridge-Playground') != '1':
            raise BridgeError('forbidden', 'The local playground header is required.')
        if self.command == 'POST' and self.headers.get('Content-Type', '').split(';', 1)[0].strip() != 'application/json':
            raise BridgeError('invalid_request', 'Send a JSON request body.')

    def _body(self):
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            raise BridgeError('invalid_request', 'Content-Length is required.') from None
        if not 0 <= length <= MAX_BODY_BYTES:
            raise BridgeError('invalid_request', 'Request body exceeds the size limit.')
        try:
            body = json.loads(self.rfile.read(length))
        except (UnicodeError, ValueError):
            raise BridgeError('invalid_request', 'Request body must be valid JSON.') from None
        if not isinstance(body, dict):
            raise BridgeError('invalid_request', 'Request body must be a JSON object.')
        return body

    def _dispatch(self, method, parts, query, body):
        bridge = self.server.bridge
        if parts == ['api', 'meta'] and method == 'GET':
            return {'workspace_path': self.server.workspace_path}
        if parts == ['api', 'capabilities'] and method == 'GET':
            return bridge.capabilities()
        if parts == ['api', 'accounts'] and method == 'GET':
            return [_account(item) for item in bridge.accounts()]
        if parts == ['api', 'accounts', 'login', 'start'] and method == 'POST':
            values = _fields(body, ('provider', 'name'))
            return bridge.account_login_start(**values)
        if len(parts) == 4 and parts[:3] == ['api', 'accounts', 'login'] and method == 'GET':
            return bridge.account_login_status(parts[3], owner_ref=query.get('owner_ref', [None])[-1])
        if len(parts) == 5 and parts[:3] == ['api', 'accounts', 'login'] and method == 'POST':
            values = _fields(body, ('owner_ref',))
            if parts[4] == 'check':
                return bridge.account_login_check(parts[3], **values)
            if parts[4] == 'complete':
                result = bridge.account_login_complete(parts[3], **values)
                return {**result, 'account': _account(result['account'])}
            if parts[4] == 'cancel':
                return bridge.account_login_cancel(parts[3], **values)
        if len(parts) == 4 and parts[:2] == ['api', 'accounts'] and method == 'GET':
            refresh = _query_flag(query, 'refresh')
            if parts[3] == 'status':
                return bridge.account_status(account_ref=parts[2], refresh=refresh)
            if parts[3] == 'usage':
                return bridge.account_usage(account_ref=parts[2], refresh=refresh)
        if len(parts) == 3 and parts[:2] == ['api', 'accounts'] and method == 'DELETE':
            return bridge.account_delete(parts[2])
        if parts == ['api', 'models'] and method == 'GET':
            return bridge.models(refresh=_query_flag(query, 'refresh'))
        if parts == ['api', 'instances'] and method == 'GET':
            return bridge.instances(limit=100)
        if parts == ['api', 'instances'] and method == 'POST':
            values = _fields(body, ('model',), ('account_ref', 'provider',
                                               'workspace_path', 'idempotency_key'))
            values.setdefault('workspace_path', self.server.workspace_path)
            return bridge.instance_create(**values)
        if len(parts) == 3 and parts[:2] == ['api', 'instances'] and method == 'POST':
            values = _fields(body, ('expected_version',), ('model', 'provider'))
            return bridge.instance_update(parts[2], **values)
        if len(parts) == 3 and parts[:2] == ['api', 'instances'] and method == 'GET':
            instance = bridge.instance_get(parts[2], include_last_turn=True)
            last_turn = instance.get('last_turn')
            return {**instance, 'last_turn': (
                {'turn_id': last_turn['id'], 'state': last_turn['state']}
                if last_turn else None)}
        if len(parts) == 4 and parts[:2] == ['api', 'instances'] and method == 'GET':
            if parts[3] == 'messages':
                return bridge.messages(parts[2], limit=200)
            if parts[3] == 'events':
                return bridge.instance_events(parts[2], after_seq=_query_number(query, 'after_seq', 0),
                                              limit=200)
        if len(parts) == 4 and parts[:2] == ['api', 'instances'] and parts[3] == 'messages' and method == 'POST':
            values = _fields(body, ('content',), ('model', 'effort', 'context_window',
                              'permission_mode', 'sandbox_mode', 'timeout_ms',
                              'idempotency_key'))
            return bridge.message_create(parts[2], **values)
        if len(parts) == 3 and parts[:2] == ['api', 'turns'] and method == 'GET':
            return bridge.turn(parts[2], include_usage=True, include_error=True)
        if len(parts) == 4 and parts[:2] == ['api', 'turns'] and parts[3] == 'events' and method == 'GET':
            return bridge.turn_events(parts[2], after_seq=_query_number(query, 'after_seq', 0), limit=200)
        if len(parts) == 4 and parts[:2] == ['api', 'turns'] and parts[3] == 'stop' and method == 'POST':
            values = _fields(body, (), ('wait', 'reason'))
            if 'wait' in values and type(values['wait']) is not bool:
                raise BridgeError('invalid_request', 'wait must be true or false.')
            return bridge.turn_stop(parts[2], **values)
        if len(parts) == 5 and parts[:2] == ['api', 'turns'] and parts[3] == 'permissions' and method == 'POST':
            values = _fields(body, ('decision',), ('reason',))
            return bridge.permission_respond(parts[2], parts[4], **values)
        raise BridgeError('not_found', 'The playground route does not exist.')

    def _static(self, path):
        name = 'index.html' if path == '/' else path.removeprefix('/static/').lstrip('/')
        root = self.server.static_root
        target = (root / name).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise BridgeError('not_found', 'The playground file does not exist.')
        content = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        self.send_response(200)
        self._headers(content_type, len(content))
        self.end_headers()
        self.wfile.write(content)

    def _headers(self, content_type, length):
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')

    def _json(self, status, value):
        content = json.dumps(value, ensure_ascii=False, allow_nan=False, default=self._serial).encode()
        self.send_response(status)
        self._headers('application/json; charset=utf-8', len(content))
        self.end_headers()
        self.wfile.write(content)

    @staticmethod
    def _serial(value):
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, Path):
            return str(value)
        raise TypeError(type(value).__name__)


for _method in ('GET', 'POST', 'DELETE', 'OPTIONS'):
    setattr(PlaygroundHandler, 'do_' + _method,
            getattr(PlaygroundHandler, 'handle_' + _method.lower()))


def create_server(root=None, *, port=8765, bridge=None, static_root=STATIC_ROOT,
                  workspace_path=None):
    return PlaygroundServer(('127.0.0.1', port), bridge or Bridge(root),
                            static_root=static_root, workspace_path=workspace_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local AgentBridge playground')
    parser.add_argument('--root', help='AgentBridge state directory (default: XDG state)')
    parser.add_argument('--port', type=int, default=8765, help='Local HTTP port')
    parser.add_argument('--workspace-path', default='.', help='Default workspace directory')
    options = parser.parse_args(argv)
    if not 0 <= options.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    with create_server(options.root, port=options.port,
                       workspace_path=options.workspace_path) as server:
        print(f'AgentBridge playground: http://127.0.0.1:{server.server_port}/')
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass
        server.bridge.close()


if __name__ == '__main__':
    main()
