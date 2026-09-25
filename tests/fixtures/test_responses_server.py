"""Loopback Responses and management fixture for the actual bundled Codex."""

from contextlib import contextmanager
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread


def message_text(message):
    content = message.get('content', [])
    if isinstance(content, str):
        return content
    return ''.join(part.get('text', '') for part in content if isinstance(part, dict))


def response_events(answer, index):
    """Emit only public assistant text; the fixture never generates reasoning."""
    response_id, item_id = f'fixture-response-{index}', f'fixture-message-{index}'
    part = {'type': 'output_text', 'text': answer, 'annotations': []}
    item = {'id': item_id, 'type': 'message', 'role': 'assistant',
            'status': 'completed', 'content': [part]}
    response = {'id': response_id, 'object': 'response', 'created_at': 1,
                'status': 'in_progress', 'output': []}
    yield {'type': 'response.created', 'response': response}
    yield {'type': 'response.output_item.added', 'output_index': 0,
           'item': {**item, 'status': 'in_progress', 'content': []}}
    yield {'type': 'response.content_part.added', 'item_id': item_id,
           'output_index': 0, 'content_index': 0, 'part': {**part, 'text': ''}}
    yield {'type': 'response.output_text.delta', 'item_id': item_id,
           'output_index': 0, 'content_index': 0, 'delta': answer}
    yield {'type': 'response.output_text.done', 'item_id': item_id,
           'output_index': 0, 'content_index': 0, 'text': answer}
    yield {'type': 'response.content_part.done', 'item_id': item_id,
           'output_index': 0, 'content_index': 0, 'part': part}
    yield {'type': 'response.output_item.done', 'output_index': 0, 'item': item}
    yield {'type': 'response.completed', 'response': {
        **response, 'status': 'completed', 'output': [item],
        'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
                  'input_tokens_details': {'cached_tokens': 0}}}}


@contextmanager
def responses_server(account_id, provider, models, *, tool_command=None):
    """Keep request evidence in memory and expose only synthetic public text."""
    observed = []
    config = {key: [] for key in (
        'gemini-api-key', 'interactions-api-key', 'claude-api-key', 'codex-api-key',
        'xai-api-key', 'meta-api-key', 'vertex-api-key', 'openai-compatibility',
    )}
    config['plugins'] = {'enabled': False}

    class Handler(BaseHTTPRequestHandler):
        def handle_get(self):
            if self.path == '/v0/management/auth-files':
                body = {'files': [{
                    'name': 'fixture.json', 'provider': provider, 'source': 'file',
                    'auth_index': account_id, 'account_type': 'oauth',
                    'email': account_id + '@fixture.invalid',
                    'id_token': {'chatgpt_account_id': account_id},
                    'runtime_only': False, 'status': 'active', 'disabled': False,
                    'unavailable': False, 'cooldowns': [],
                }]}
            elif self.path == '/v0/management/config':
                body = config
            elif self.path == '/v0/management/auth-files/models?name=fixture.json':
                body = {'models': [{'id': model} for model in models]}
            elif self.path.startswith('/v1/models'):
                body = {'models': [{'id': model, 'slug': model} for model in models]}
            else:
                self.send_error(404)
                return
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def handle_post(self):
            if self.path != '/v1/responses':
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers['Content-Length']))
            if self.headers.get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            request = json.loads(raw)
            messages = [item for item in request.get('input', []) if isinstance(item, dict)]
            prompts = [message_text(item) for item in messages if item.get('role') == 'user'
                       and message_text(item).startswith('fixture request ')]
            answers = [message_text(item) for item in messages if item.get('role') == 'assistant']
            tool_names = [item.get('name') for item in request.get('tools', [])
                          if isinstance(item, dict) and item.get('name')]
            tool_outputs = [item.get('output') for item in messages
                            if item.get('type') == 'function_call_output']
            observation = {'account_id': account_id, 'model': request['model'],
                           'prompts': prompts, 'answers': answers,
                           'tool_names': tool_names, 'tool_outputs': tool_outputs}
            observed.append(observation)
            answer = f'fixture answer {len(prompts)} via {account_id}'
            events = (tool_events(tool_command, tool_names) if tool_command and not tool_outputs
                      else response_events(answer, len(observed)))
            encoded = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event)
                              + '\n\n' for event in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):
            pass

    setattr(Handler, 'do_GET', Handler.handle_get)
    setattr(Handler, 'do_POST', Handler.handle_post)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', observed
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def tool_events(command, tool_names):
    """Select a real advertised native shell tool for a synthetic canary probe."""
    if 'shell_command' in tool_names:
        name, arguments = 'shell_command', {'command': command}
    elif 'exec_command' in tool_names:
        name, arguments = 'exec_command', {'cmd': command, 'tty': False,
                                          'yield_time_ms': 1000, 'max_output_tokens': 1000}
    else:
        name, arguments = 'shell', {'command': ['/bin/sh', '-c', command], 'timeout_ms': 1000}
    assert name in tool_names, tool_names
    item = {'id': 'fixture-function-item', 'type': 'function_call', 'call_id': 'fixture-call',
            'name': name, 'arguments': json.dumps(arguments), 'status': 'completed'}
    response = {'id': 'fixture-tool-response', 'object': 'response', 'created_at': 1,
                'status': 'in_progress', 'output': []}
    yield {'type': 'response.created', 'response': response}
    yield {'type': 'response.output_item.added', 'output_index': 0,
           'item': {**item, 'status': 'in_progress', 'arguments': ''}}
    yield {'type': 'response.function_call_arguments.delta', 'output_index': 0,
           'item_id': item['id'], 'delta': item['arguments']}
    yield {'type': 'response.function_call_arguments.done', 'output_index': 0,
           'item_id': item['id'], 'arguments': item['arguments']}
    yield {'type': 'response.output_item.done', 'output_index': 0, 'item': item}
    yield {'type': 'response.completed', 'response': {
        **response, 'status': 'completed', 'output': [item],
        'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}}
