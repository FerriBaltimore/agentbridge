"""Claude Code stream-json input and host permission response protocol."""
from .attachments import images
from .errors import BridgeError
import time


def initialize(channel, timeout=30):
    deadline = time.monotonic() + timeout
    channel.send({'type': 'control_request', 'request_id': 'initialize',
                  'request': {'subtype': 'initialize', 'hooks': {}}})
    while True:
        value = channel.receive(max(0, deadline - time.monotonic()))
        response = value.get('response') or {}
        if value.get('type') == 'control_response' and response.get('request_id') == 'initialize':
            if response.get('subtype') != 'success':
                raise BridgeError('provider_failed', 'Claude rejected initialization.')
            return response.get('response') or {}


def execute(channel, payload, emit, approve):
    initialize(channel)
    content = [{'type': 'text', 'text': payload['prompt']}]
    content.extend({'type': 'image', 'source': {'type': 'base64', 'media_type': item['media_type'],
                                             'data': item['data']}}
                   for item in images(payload['options'].get('attachments', [])))
    channel.send({'type': 'user', 'session_id': payload.get('native_id') or '',
                  'parent_tool_use_id': None, 'message': {'role': 'user', 'content': content}})
    while True:
        value = channel.receive(payload['options']['timeout'])
        if value.get('type') == 'control_request':
            request = value.get('request') or {}
            request_id = value.get('request_id')
            if request.get('subtype') == 'can_use_tool':
                original = request.get('input') or {}
                def respond(decision):
                    result = {'behavior': 'allow', 'updatedInput': original} if decision == 'allow' else {
                        'behavior': 'deny', 'message': 'The host denied this request.'}
                    channel.send({'type': 'control_response', 'response': {
                        'subtype': 'success', 'request_id': request_id, 'response': result}})
                approve({'operation': request.get('tool_name'), 'input': original}, respond)
            else:
                channel.send({'type': 'control_response', 'response': {
                    'subtype': 'error', 'request_id': request_id, 'error': 'Unsupported host interaction.'}})
        elif value.get('type') not in {'control_response', 'control_cancel_request'}:
            emit(value)
            if value.get('type') == 'result':
                return
