"""Codex app-server execution and one-request approvals over its native protocol."""
import time
from .attachments import images
from .errors import BridgeError
from .provider_errors import normalize
from .session_events import routing
from .codex_skills import selected_config
from .codex_notices import notice
from .native_sessions import validate_native_id
from .native_observations import event as native_event


class CodexControl:
    def __init__(self, channel, payload, emit, approve, steering=None):
        self.channel, self.payload, self.emit, self.approve = channel, payload, emit, approve
        self.next_id, self.done, self.thread_id, self.turn_id = 0, False, None, None
        self.steering, self.pending_input = steering, None
        self.native_control = None

    def rpc(self, method, params, *, timeout=None):
        try:
            self.next_id += 1
            request_id = self.next_id
            self.channel.send({'id': request_id, 'method': method, 'params': params})
            deadline = time.monotonic() + (timeout if timeout is not None
                                            else self.payload['options']['timeout'])
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError('provider_timeout', 'The native provider did not answer in time.',
                                      phase='execution', outcome='unknown')
                value = self.channel.receive(remaining)
                if value.get('id') == request_id and 'method' not in value:
                    if 'error' in value:
                        error = value['error']
                        native_code = error.get('code') if isinstance(error, dict) else None
                        details = ({'native_code': native_code} if type(native_code) is int
                                   and -(2 ** 31) <= native_code < 2 ** 31 else {})
                        if (method == 'thread/resume' and isinstance(error, dict)
                                and error.get('code') == -32600
                                and error.get('message') == 'no rollout found for thread id '
                                + str(params.get('threadId'))):
                            raise BridgeError('native_thread_missing',
                                              'The native runtime confirmed this thread is absent.',
                                              details=details)
                        issue = normalize('codex', value['error'], phase='launch', outcome='not_started')
                        raise BridgeError(issue['code'], 'Codex rejected the native operation.',
                                          phase='launch', outcome='not_started', retryable=False,
                                          details={**issue['details'], **details})
                    result = value.get('result')
                    if not isinstance(result, dict):
                        raise BridgeError('provider_protocol_error', 'Invalid native response result.',
                                          phase='execution', outcome='unknown')
                    return result
                self.event(value)
        except BridgeError as error:
            if method in {'initialize', 'thread/resume', 'thread/read',
                          'turn/start', 'turn/steer', 'turn/interrupt'}:
                error.details = {**error.details, 'native_method': method.replace('/', '_')}
            raise

    def event(self, value):
        if 'method' not in value and 'id' in value:
            if self.pending_input and value['id'] == self.pending_input[0]:
                _, message_id = self.pending_input
                if 'error' in value:
                    self.steering.acknowledge(message_id, accepted=False, code='steering_rejected')
                elif isinstance(value.get('result'), dict) and value['result'].get('turnId') == self.turn_id:
                    self.steering.acknowledge(message_id, accepted=True)
                else:
                    self.steering.acknowledge(message_id, accepted=None, code='unknown_outcome')
                self.pending_input = None
            # A query whose caller timed out can still reply. It is not a native
            # notification and cannot change the observed execution state.
            return
        method, params = value.get('method'), value.get('params', {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise BridgeError('provider_protocol_error', 'Invalid native notification parameters.',
                              phase='execution', outcome='unknown')
        if 'id' in value and method:
            if (method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}
                    and params.get('threadId') == self.thread_id
                    and (self.turn_id is None or params.get('turnId') == self.turn_id)):
                self.approve({'operation': method, 'input': {k: params[k] for k in
                              ('command', 'cwd', 'reason', 'itemId', 'networkApprovalContext', 'grantRoot') if k in params}},
                             lambda decision: self.channel.send({'id': value['id'], 'result': {
                                 'decision': 'accept' if decision == 'allow' else 'decline'}}))
            else:
                self.channel.send({'id': value['id'], 'error': {'code': -32601,
                                   'message': 'This host does not support the requested interaction.'}})
            return
        if params.get('threadId') not in (None, self.thread_id):
            return
        if self.turn_id and params.get('turnId') not in (None, self.turn_id):
            return
        native = native_event(method, params)
        if native is not None:
            self.emit(native)
        observed = notice(method, params)
        if observed is not None:
            if (observed['kind'] == 'mcp_startup' and self.payload.get('mcp_enabled')
                    and params.get('name') in ({server['name'] for server in
                        (self.payload.get('mcp') or {}).get('servers', [])}
                        if (self.payload.get('mcp') or {}).get('version') == 2
                        else {'agentbridge_execution'})
                    and observed['status'] in {'failed', 'cancelled'}):
                raise BridgeError('provider_unavailable', 'The selected MCP server could not start.',
                                  phase='execution' if self.turn_id else 'launch',
                                  outcome='unknown' if self.turn_id else 'not_started')
            self.emit(observed)
            return
        if method == 'turn/completed':
            turn = params.get('turn', {})
            if not isinstance(turn, dict):
                raise BridgeError('provider_protocol_error', 'Invalid native completed turn.',
                                  phase='execution', outcome='unknown')
            if self.turn_id and turn.get('id') != self.turn_id:
                return
            status = turn.get('status')
            if status not in {'completed', 'interrupted', 'failed'}:
                self.emit({'type': 'turn.failed', 'error': normalize('codex',
                    {'code': 'provider_protocol_error'}, outcome='unknown')})
                self.done = True
                return
            event = {'type': 'turn.' + status}
            if turn.get('error'):
                event['error'] = normalize('codex', turn['error'])
            self.emit(event)
            self.done = True
        elif method == 'error':
            self.emit({'type': 'error', 'error': normalize('codex', params.get('error'),
                       terminal=False, provider_retrying=params.get('willRetry') is True),
                       'will_retry': params.get('willRetry') is True})
        elif method == 'account/rateLimits/updated' and isinstance(params.get('rateLimits'), dict):
            self.emit({'type': 'bridge_quota', 'limits': {'rateLimits': params['rateLimits']}})
        elif method == 'model/rerouted':
            self.emit({'type': 'bridge_model_changed', 'change': routing('codex', params)})
        elif method == 'item/agentMessage/delta':
            self.emit({'type': 'bridge_text_delta', 'text': params.get('delta', '')})
        elif method in {'item/started', 'item/completed'}:
            item = params.get('item', {})
            if not isinstance(item, dict):
                raise BridgeError('provider_protocol_error', 'Invalid native item.',
                                  phase='execution', outcome='unknown')
            kinds = {'agentMessage': 'agent_message', 'userMessage': 'user_message',
                     'commandExecution': 'command_execution',
                     'fileChange': 'file_change', 'mcpToolCall': 'mcp_tool_call', 'webSearch': 'web_search',
                     'dynamicToolCall': 'dynamic_tool_call', 'collabAgentToolCall': 'collab_agent_tool_call',
                     'contextCompaction': 'context_compaction', 'reasoning': 'reasoning'}
            kind = kinds.get(item.get('type'), item.get('type'))
            if kind and kind != 'reasoning':
                safe = {k: item[k] for k in ('id', 'text', 'command', 'cwd', 'changes', 'status',
                                           'server', 'tool', 'arguments', 'result', 'query', 'success') if k in item}
                for native, public in [('aggregatedOutput', 'aggregated_output'), ('exitCode', 'exit_code')]:
                    if native in item:
                        safe[public] = item[native]
                if item.get('error'):
                    safe['error'] = normalize('codex', item['error'])
                for native, public in [('contentItems', 'content'), ('agentsStates', 'agents_states'),
                                       ('receiverThreadIds', 'receiver_thread_ids'),
                                       ('senderThreadId', 'sender_thread_id')]:
                    if native in item:
                        safe[public] = item[native]
                self.emit({'type': method.replace('/', '.'), 'item': {'type': kind, **safe}})
        elif method == 'thread/tokenUsage/updated':
            usage = params.get('tokenUsage', {})
            if not isinstance(usage, dict):
                raise BridgeError('provider_protocol_error', 'Invalid native token observation.',
                                  phase='execution', outcome='unknown')
            for key, scope, aggregation in [('total', 'session', 'cumulative'), ('last', 'observation', 'latest')]:
                counters = usage.get(key)
                if not isinstance(counters, dict):
                    continue
                tokens = {public: counters[native] for native, public in
                          [('inputTokens', 'input_tokens'), ('outputTokens', 'output_tokens'),
                           ('cachedInputTokens', 'cached_input_tokens'), ('totalTokens', 'total_tokens'),
                           ('reasoningOutputTokens', 'reasoning_output_tokens'),
                           ('cacheWriteInputTokens', 'cache_write_input_tokens')]
                          if isinstance(counters.get(native), int) and not isinstance(counters[native], bool)
                          and counters[native] >= 0}
                context_window = usage.get('modelContextWindow')
                if type(context_window) is not int or context_window <= 0:
                    context_window = None
                if tokens:
                    self.emit({'type': 'bridge_usage', 'scope': scope, 'tokens': tokens,
                               'aggregation': aggregation, 'context_window': context_window})
        elif native is None and method and not (method.startswith('item/reasoning/') or method in {
            'thread/started', 'thread/status/changed', 'turn/started', 'item/started',
        }):
            self.emit({'type': 'bridge_gap'})

    def execute(self):
        options = self.payload['options']
        self.rpc('initialize', {'clientInfo': {'name': 'agentbridge', 'version': '2.3.1'}})
        self.channel.send({'method': 'initialized', 'params': {}})
        if self.payload.get('context_package') is not None:
            listing = self.rpc('skills/list', {'cwds': [self.payload['cwd']], 'forceReload': True})
            self.payload['codex_config']['skills.config'] = selected_config(
                listing, self.payload['skill_inputs'], self.payload['cwd'])
        params = {'cwd': self.payload['cwd'], 'approvalPolicy': 'on-request'
                  if options['permission_mode'] == 'default' else 'never',
                  'approvalsReviewer': 'user', 'sandbox': options['sandbox']}
        if self.payload.get('developer_instructions') is not None:
            params['developerInstructions'] = self.payload['developer_instructions']
        if self.payload.get('codex_config'):
            params['config'] = self.payload['codex_config']
        if self.payload.get('model'):
            params['model'] = self.payload['model']
        native_id = self.payload.get('native_id')
        if native_id is not None:
            validate_native_id(native_id)
        execution_mode = (self.payload.get('context_package') or {}).get('execution_mode')
        if execution_mode == 'evaluation_inputs_only' and native_id:
            raise BridgeError('invalid_context', 'Evaluation context cannot resume a native thread.',
                              phase='launch', outcome='not_started')
        if execution_mode == 'evaluation_inputs_only':
            params['ephemeral'] = True
        if native_id:
            params['threadId'] = native_id
        result = self.rpc('thread/resume' if native_id else 'thread/start', params)
        thread = result.get('thread')
        self.thread_id = validate_native_id(thread.get('id') if isinstance(thread, dict) else None)
        if native_id is not None and self.thread_id != native_id:
            raise BridgeError('native_session_diverged',
                              'Codex resumed a different native conversation.',
                              phase='launch', outcome='not_started')
        self.emit({'type': 'thread.started', 'thread_id': self.thread_id})
        content = [{'type': 'text', 'text': self.payload['prompt']}]
        content.extend(self.payload.get('skill_inputs', []))
        content.extend(self.payload.get('evidence_inputs', []))
        content.extend({'type': 'image', 'url': 'data:' + item['media_type'] + ';base64,' + item['data']}
                       for item in images(options.get('attachments', [])))
        params = {'threadId': self.thread_id, 'input': content}
        if options.get('effort'):
            params['effort'] = options['effort']
        result = self.rpc('turn/start', params)
        self.turn_id = result['turn']['id']
        while not self.done:
            if self.native_control is not None:
                self.native_control.poll()
                if self.done:
                    break
            if self.steering is None:
                self.event(self.channel.receive(options['timeout']))
                continue
            self.live_input()
            try:
                value = self.channel.receive(.1)
            except BridgeError as error:
                if error.code == 'provider_timeout':
                    continue
                raise
            self.event(value)

    def live_input(self):
        if self.pending_input:
            return
        item = self.steering.take()
        if item is None:
            return
        message_id, content = item
        self.next_id += 1
        self.pending_input = (self.next_id, message_id)
        self.channel.send({'id': self.next_id, 'method': 'turn/steer', 'params': {
            'threadId': self.thread_id, 'expectedTurnId': self.turn_id, 'input': content}})
