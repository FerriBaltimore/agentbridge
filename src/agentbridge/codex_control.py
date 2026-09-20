"""Codex app-server execution and one-request approvals over its native protocol."""
from .attachments import images
from .errors import BridgeError


class CodexControl:
    def __init__(self, channel, payload, emit, approve):
        self.channel, self.payload, self.emit, self.approve = channel, payload, emit, approve
        self.next_id, self.done, self.thread_id, self.turn_id = 0, False, None, None

    def rpc(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.channel.send({'id': request_id, 'method': method, 'params': params})
        while True:
            value = self.channel.receive(self.payload['options']['timeout'])
            if value.get('id') == request_id and 'method' not in value:
                if 'error' in value:
                    raise BridgeError('provider_failed', 'Codex rejected the native operation.')
                return value.get('result', {})
            self.event(value)

    def event(self, value):
        method, params = value.get('method'), value.get('params') or {}
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
        if method == 'turn/completed':
            turn = params.get('turn') or {}
            if self.turn_id and turn.get('id') != self.turn_id:
                return
            success = turn.get('status') == 'completed'
            self.emit({'type': 'turn.completed' if success else 'turn.failed'})
            self.done = True
        elif method == 'item/agentMessage/delta':
            self.emit({'type': 'bridge_text_delta', 'text': params.get('delta', '')})
        elif method in {'item/started', 'item/completed'}:
            item = params.get('item') or {}
            kinds = {'agentMessage': 'agent_message', 'commandExecution': 'command_execution',
                     'fileChange': 'file_change', 'mcpToolCall': 'mcp_tool_call', 'webSearch': 'web_search',
                     'reasoning': 'reasoning'}
            kind = kinds.get(item.get('type'), item.get('type'))
            if kind and kind != 'reasoning':
                safe = {k: item[k] for k in ('id', 'text', 'command', 'cwd', 'changes', 'status',
                                           'server', 'tool', 'arguments', 'result') if k in item}
                for native, public in [('aggregatedOutput', 'aggregated_output'), ('exitCode', 'exit_code')]:
                    if native in item:
                        safe[public] = item[native]
                self.emit({'type': method.replace('/', '.'), 'item': {'type': kind, **safe}})
        elif method == 'thread/tokenUsage/updated':
            total = (params.get('tokenUsage') or {}).get('total')
            if isinstance(total, dict):
                tokens = {public: total[native] for native, public in
                          [('inputTokens', 'input_tokens'), ('outputTokens', 'output_tokens'),
                           ('cachedInputTokens', 'cached_input_tokens')] if native in total}
                self.emit({'type': 'bridge_usage', 'scope': 'session', 'tokens': tokens})

    def execute(self):
        options = self.payload['options']
        self.rpc('initialize', {'clientInfo': {'name': 'agentbridge', 'version': '0.1.0'}})
        self.channel.send({'method': 'initialized', 'params': {}})
        params = {'cwd': self.payload['cwd'], 'approvalPolicy': 'on-request'
                  if options['permission_mode'] == 'default' else 'never',
                  'approvalsReviewer': 'user', 'sandbox': options['sandbox']}
        if self.payload.get('model'):
            params['model'] = self.payload['model']
        native_id = self.payload.get('native_id')
        if native_id:
            params['threadId'] = native_id
        result = self.rpc('thread/resume' if native_id else 'thread/start', params)
        self.thread_id = result['thread']['id']
        self.emit({'type': 'thread.started', 'thread_id': self.thread_id})
        content = [{'type': 'text', 'text': self.payload['prompt']}]
        content.extend({'type': 'image', 'url': 'data:' + item['media_type'] + ';base64,' + item['data']}
                       for item in images(options.get('attachments', [])))
        params = {'threadId': self.thread_id, 'input': content}
        if options.get('effort'):
            params['effort'] = options['effort']
        result = self.rpc('turn/start', params)
        self.turn_id = result['turn']['id']
        while not self.done:
            self.event(self.channel.receive(options['timeout']))
