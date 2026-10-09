"""Translate native observations without deriving an execution state."""

import re

from .errors import BridgeError
from .provider_errors import normalize


def snake(value):
    return re.sub(r'(?<!^)(?=[A-Z])', '_', value).lower()


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise BridgeError('provider_protocol_error', 'Missing native observation identity.')
    return value


def status(value):
    if isinstance(value, str):
        return snake(value)
    if isinstance(value, dict) and isinstance(value.get('type'), str):
        result = {'type': snake(value['type'])}
        if isinstance(value.get('activeFlags'), list):
            result['active_flags'] = [snake(flag) for flag in value['activeFlags']
                                      if isinstance(flag, str)]
        return result
    raise BridgeError('provider_protocol_error', 'Missing native execution status.')


ITEM_FIELDS = {
    'text': 'text', 'phase': 'phase', 'command': 'command', 'cwd': 'cwd',
    'changes': 'changes', 'server': 'server', 'tool': 'tool', 'arguments': 'arguments',
    'result': 'result', 'query': 'query', 'success': 'success', 'content': 'content',
    'contentItems': 'content', 'aggregatedOutput': 'output', 'exitCode': 'exit_code',
    'durationMs': 'duration_ms', 'receiverThreadIds': 'receiver_thread_ids',
    'senderThreadId': 'sender_thread_id', 'action': 'action',
}

ITEM_TYPES = {'agentMessage', 'userMessage', 'commandExecution', 'fileChange',
              'mcpToolCall', 'dynamicToolCall', 'collabAgentToolCall', 'webSearch',
              'imageView', 'imageGeneration', 'enteredReviewMode', 'exitedReviewMode',
              'contextCompaction'}


def item(value):
    if not isinstance(value, dict) or not isinstance(value.get('type'), str):
        raise BridgeError('provider_protocol_error', 'Invalid native item.')
    if value['type'] == 'reasoning':
        return None
    result = {'item_id': identifier(value.get('id')), 'type': snake(value['type'])}
    if value['type'] not in ITEM_TYPES:
        return {**result, 'unsupported': True}
    result.update({public: value[native] for native, public in ITEM_FIELDS.items()
                   if native in value})
    if 'status' in value:
        result['status'] = status(value['status'])
    if value.get('error'):
        result['error'] = normalize('codex', value['error'])
    if isinstance(value.get('agentsStates'), dict):
        result['agents_states'] = {agent: {'status': snake(state['status'])}
            for agent, state in value['agentsStates'].items()
            if isinstance(state, dict) and isinstance(state.get('status'), str)}
    return result


def turn(value):
    if not isinstance(value, dict):
        raise BridgeError('provider_protocol_error', 'Invalid native turn.')
    result = {'native_turn_id': identifier(value.get('id')), 'status': status(value.get('status'))}
    if 'items' in value:
        if not isinstance(value['items'], list):
            raise BridgeError('provider_protocol_error', 'Invalid native turn items.')
        result['items'] = [mapped for raw in value['items'] if (mapped := item(raw)) is not None]
    if value.get('error'):
        result['error'] = normalize('codex', value['error'])
    return result


def thread(value):
    if not isinstance(value, dict):
        raise BridgeError('provider_protocol_error', 'Invalid native thread.')
    result = {'native_session_id': identifier(value.get('id')),
              'status': status(value.get('status')), 'source': 'native'}
    for native, public in (('createdAt', 'created_at'), ('updatedAt', 'updated_at'),
                           ('name', 'name'), ('cwd', 'workspace_path')):
        if native in value:
            result[public] = value[native]
    if 'turns' in value:
        if not isinstance(value['turns'], list):
            raise BridgeError('provider_protocol_error', 'Invalid native thread turns.')
        result['turns'] = [turn(raw) for raw in value['turns']]
    return result


def event(method, params):
    """Keep explicit native identity and progress; never publish private reasoning."""
    kinds = {'thread/status/changed': 'native_thread_status',
             'turn/started': 'native_turn_started', 'turn/completed': 'native_turn_completed',
             'item/started': 'native_item_started', 'item/completed': 'native_item_completed',
             'item/agentMessage/delta': 'native_item_delta',
             'item/commandExecution/outputDelta': 'native_item_delta',
             'item/fileChange/outputDelta': 'native_item_delta',
             'item/mcpToolCall/progress': 'native_item_progress',
             'turn/plan/updated': 'native_plan_updated'}
    kind = kinds.get(method)
    if kind is None:
        return None
    data = {'source': 'native'}
    for native, public in (('threadId', 'native_session_id'), ('turnId', 'native_turn_id'),
                           ('itemId', 'item_id')):
        if params.get(native) is not None:
            data[public] = identifier(params[native])
    if method == 'thread/status/changed':
        data['status'] = status(params.get('status'))
    elif method in {'turn/started', 'turn/completed'}:
        if isinstance(params.get('turn'), dict) and params['turn'].get('id') is None:
            return None  # Preserve legacy launch-error evidence without inventing a native ID.
        data['turn'] = turn(params.get('turn'))
        data['native_turn_id'] = data['turn']['native_turn_id']
    elif method in {'item/started', 'item/completed'}:
        raw = params.get('item')
        if isinstance(raw, dict) and (raw.get('type') == 'userMessage'
                                     or raw.get('type') not in ITEM_TYPES):
            return None  # Input echoes and unknown payloads retain their existing safe notices/gaps.
        mapped = item(params.get('item'))
        if mapped is None:
            return None
        data['item'], data['item_id'] = mapped, mapped['item_id']
    elif method.endswith('/delta') or method.endswith('/outputDelta'):
        data['field'] = 'text' if method == 'item/agentMessage/delta' else 'output'
        data['delta'] = params.get('delta', '')
    elif method == 'item/mcpToolCall/progress':
        data['message'] = params.get('message', '')
    else:
        data['plan'] = params.get('plan', [])
        if params.get('explanation') is not None:
            data['explanation'] = params['explanation']
    return {'type': 'bridge_native', 'kind': kind, 'data': data}
