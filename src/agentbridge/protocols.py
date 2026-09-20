"""Translate observable provider protocols; preserve gaps instead of inventing data."""
from .models import ENGINES
from .provider_errors import normalize
from .session_events import identifier, retry, routing

SUCCESS = {'completed','success','succeeded','finished','ok'}
FAILURE = {'failed','error','errored','killed','cancelled','canceled','stopped'}


class Parser:
    def __init__(self, engine, emit, error_handler=None):
        if engine not in ENGINES:
            raise ValueError(engine)
        self.engine, self.emit = engine, emit
        self.terminal = None
        self.open_tools = {}
        self.tasks = {}
        self.started_tools = set()
        self.completed_tools = set()
        self.failed = False
        self.last_error = None
        self.error_handler = error_handler

    def event(self, kind, data):
        self.emit(kind, data)

    def error(self, value, *, terminal=True, provider_retrying=False, outcome=None, phase='execution'):
        issue = normalize(self.engine, value, terminal=terminal, outcome=outcome,
                          provider_retrying=provider_retrying, phase=phase)
        if self.error_handler:
            issue = self.error_handler(issue)
        return self.record_error(issue, terminal=terminal, provider_retrying=provider_retrying)

    def record_error(self, issue, *, terminal=True, provider_retrying=False):
        issue = {**issue, 'terminal': terminal, 'provider_retrying': provider_retrying}
        self.last_error = issue
        if terminal:
            self.failed = True
        self.event('error', issue)
        return issue

    def feed(self, event):
        if not isinstance(event, dict):
            self.event('gap', {'reason':'non_object_event'})
            return
        if event.get('type') == 'bridge_error':
            value = event.get('error') or event
            phase = value.get('phase') if isinstance(value, dict) else None
            self.error(value, outcome=event.get('outcome', 'unknown'),
                       phase=phase if phase in {'admission', 'launch', 'execution', 'recovery'} else 'execution')
            return
        getattr(self, '_'+self.engine)(event)

    def tool(self, id, name, arguments=None, *, done=False, result=None, status=None, parent=None):
        if not id:
            self.event('gap',{'reason':'missing_tool_id','name':name})
            return
        id = str(id)
        key = (str(parent or ''), id)
        fields = {'call_id':id,'name':name,'parent_id':parent}
        if key not in self.started_tools:
            self.started_tools.add(key)
            self.open_tools[key] = fields
            self.event('tool_call',{**fields,'input':arguments})
        if done and key not in self.completed_tools:
            self.completed_tools.add(key)
            self.open_tools.pop(key,None)
            outcome = 'failed' if status in FAILURE else 'completed' if status in SUCCESS else 'unknown'
            self.event('tool_result',{**fields,'output':result,'outcome':outcome})

    def task(self, id, status, **extra):
        if not id:
            self.event('gap',{'reason':'missing_task_id','status':status})
            return
        self.tasks[str(id)] = status
        self.event('subagent',{'agent_id':str(id),'status':status,**extra})

    def end(self):
        for fields in self.open_tools.values():
            self.event('tool_result',{**fields,'outcome':'unknown','reason':'stream_ended_without_result'})
        pending = [id for id,status in self.tasks.items() if status not in SUCCESS | FAILURE]
        for id in pending:
            self.event('subagent',{'agent_id':id,'status':'unknown','reason':'stream_ended_without_result'})
        unresolved = bool(self.open_tools or pending)
        self.open_tools.clear()
        return unresolved

    def _codex(self, ev):
        t=ev.get('type','')
        if t == 'bridge_text_delta':
            self.event('text_delta', {'text': ev.get('text', '')})
        elif t == 'bridge_usage':
            self.event('usage', {'source': 'codex_app_server', 'scope': ev['scope'], 'tokens': ev.get('tokens'),
                                'aggregation': ev.get('aggregation'), 'context_window': ev.get('context_window')})
        elif t == 'bridge_quota':
            self.event('quota', {'source': 'codex_app_server', 'scope': 'account', 'limits': ev.get('limits')})
        elif t == 'bridge_model_changed':
            self.event('model_changed', ev['change'])
        elif t == 'thread.started':
            self.event('session',{'native_id':ev.get('thread_id')})
        elif t in ('turn.started','turn.completed','turn.failed','turn.interrupted'):
            status=t.split('.')[1]
            self.event('turn',{'status':status})
            if status != 'started':
                self.terminal=status
                self.failed |= status == 'failed'
            if isinstance(ev.get('usage'),dict):
                self.event('usage',{'source':'codex_exec','scope':'turn','tokens':ev['usage']})
            if ev.get('error'):
                self.error(ev['error'])
            elif status == 'interrupted':
                self.error({'code': 'interrupted'})
            elif status == 'failed' and not self.last_error:
                self.error(ev)
        elif t.startswith('item.'):
            item=ev.get('item') or {}
            if not isinstance(item,dict):
                self.event('gap',{'reason':'malformed_item'}); return
            typ=item.get('type')
            done=t=='item.completed'
            if typ=='agent_message' and done:
                self.event('assistant',{'text':item.get('text','')})
            elif typ in {'command_execution','file_change','patch_apply','mcp_tool_call','dynamic_tool_call','web_search'}:
                args={k:item[k] for k in ('command','cwd','changes','path','server','tool','arguments','query') if k in item}
                result={k:item[k] for k in ('aggregated_output','exit_code','result','error','changes','output','content') if k in item}
                if 'error' in result:
                    result['error'] = normalize(self.engine, result['error'])
                status=item.get('status','completed' if done else 'running')
                if item.get('exit_code') not in (None,0) or item.get('error'): status='failed'
                self.tool(item.get('id'),typ,args,done=done,result=result,status=status,parent=item.get('parent_thread_id'))
            elif typ in {'collab_agent_tool_call','collab_tool_call'}:
                ids=item.get('receiver_thread_ids') or []
                states=item.get('agents_states') or {}
                for id in ids:
                    state=states.get(id,{})
                    status=state.get('status') if isinstance(state,dict) else state
                    # Completion of a spawn call does not prove completion of the spawned agent.
                    self.task(id,status or 'unknown',parent_id=item.get('sender_thread_id'),tool=item.get('tool'))
                if not ids:self.event('gap',{'reason':'subagent_identity_unavailable'})
            elif typ in {'context_compaction','contextCompaction'} and done:
                self.event('compaction',{'observed':True})
            elif typ not in {'reasoning','agent_message'} and done:
                self.event('gap',{'reason':'unsupported_item','native_type':typ})
        elif t=='error':
            self.error(ev.get('error') or ev, terminal=False,
                       provider_retrying=ev.get('will_retry') is True)
        else:
            self.event('gap',{'reason':'unsupported_event','native_type':t})

    def _claude(self, ev):
        t=ev.get('type')
        parent=ev.get('parent_tool_use_id')
        if t=='system':
            sub=ev.get('subtype')
            if sub=='init':self.event('session',{'native_id':ev.get('session_id')})
            elif sub=='compact_boundary':self.event('compaction',{'observed':True})
            elif sub in {'task_started','task_notification','task_updated'}:
                status = ev.get('status') or (ev.get('patch') or {}).get('status') or 'started'
                self.task(ev.get('task_id'),status,parent_id=parent,description=ev.get('description'),background=ev.get('is_backgrounded'))
            elif sub=='permission_denied':self.event('permission_denied',{'reason':'provider_denied'})
            elif sub == 'api_retry':
                self.event('retry', retry('claude', ev))
            elif sub == 'model_refusal_fallback':
                self.event('model_changed', routing('claude', ev))
            elif sub == 'model_refusal_no_fallback':
                self.error({'code': 'safety_blocked'})
            else:self.event('status',{'status':ev.get('status') or sub})
        elif t in {'assistant','user'}:
            msg=ev.get('message') or {}
            content=msg.get('content',[]) if isinstance(msg,dict) else []
            if t == 'assistant' and ev.get('error'):
                # Claude wraps API errors in assistant text; it is not assistant prose.
                self.error({'code': ev['error'], 'message': content}, terminal=False)
                return
            if isinstance(content,str):content=[{'type':'text','text':content}]
            for block in content:
                if not isinstance(block,dict):continue
                typ=block.get('type')
                if typ=='text' and t=='assistant':
                    supersedes = ev.get('supersedes')
                    self.event('assistant', {'text': block.get('text', ''), 'parent_id': parent,
                        'provider_message_id': identifier(ev.get('uuid')), 'incomplete': ev.get('aborted') is True,
                        'supersedes': [item for item in supersedes[:1000] if identifier(item)]
                        if isinstance(supersedes, list) else []})
                elif typ=='tool_use':self.tool(block.get('id'),block.get('name','tool'),block.get('input'),parent=parent)
                elif typ=='tool_result':
                    self.tool(block.get('tool_use_id'),'tool',done=True,result=block.get('content'),status='failed' if block.get('is_error') else 'completed',parent=parent)
                elif typ not in {'thinking','redacted_thinking','text'}:self.event('gap',{'reason':'unsupported_block','native_type':typ})
        elif t=='stream_event':
            ev=ev.get('event') or {}
            delta=ev.get('delta') or {}
            if ev.get('type')=='content_block_delta' and delta.get('type')=='text_delta':
                self.event('text_delta',{'text':delta.get('text',''),'parent_id':parent})
            elif ev.get('type') == 'error':
                self.error(ev.get('error') or ev, terminal=False)
        elif t=='result':
            failed=bool(ev.get('is_error')) or str(ev.get('subtype','')).startswith('error')
            interrupted = ev.get('terminal_reason') in {'aborted_streaming', 'aborted_tools'}
            output_limited = ev.get('stop_reason') == 'max_tokens'
            failed |= output_limited
            self.failed |= failed
            self.terminal='interrupted' if interrupted else 'failed' if failed else 'completed'
            self.event('turn',{'status':self.terminal})
            if failed or interrupted:
                issue = normalize(self.engine, {'code': 'output_limit_exceeded'} if output_limited else ev)
                if issue['code'] == 'provider_failed' and self.last_error:
                    issue = self.last_error
                if issue is self.last_error:
                    self.record_error(issue)
                else:
                    self.error(issue, outcome=issue['outcome'])
            if ev.get('usage') or ev.get('total_cost_usd') is not None:
                self.event('usage',{'source':'claude_result','scope':'turn','tokens':ev.get('usage')})
            if ev.get('total_cost_usd') is not None or ev.get('modelUsage') or ev.get('model_usage'):
                self.event('usage', {'source': 'claude_result', 'scope': 'session',
                    'cost_usd': ev.get('total_cost_usd'), 'cost_kind': 'provider_reported',
                    'models': ev.get('modelUsage') or ev.get('model_usage'), 'aggregation': 'cumulative'})
            if ev.get('permission_denials'):self.event('permission_denied',{'count':len(ev['permission_denials'])})
        elif t=='rate_limit_event':
            self.event('quota',{'source':'claude_stream','scope':'account','limits':ev.get('rate_limit_info')})
        elif t in {'tool_progress','tool_use_summary'}:
            self.event('status',{'status':t,'call_id':ev.get('tool_use_id'),'parent_id':parent})
        else:self.event('gap',{'reason':'unsupported_event','native_type':t})

    def _cursor(self, ev):
        t=ev.get('type')
        if t=='bridge_session':self.event('session',{'native_id':ev.get('native_id')})
        elif t=='bridge_result':
            status=ev.get('status','unknown')
            self.terminal = ('completed' if status in SUCCESS else 'interrupted'
                             if status in {'cancelled', 'canceled', 'expired'} else 'failed')
            self.failed |= self.terminal=='failed'
            self.event('turn',{'status':self.terminal})
            if self.terminal != 'completed':
                self.error(ev.get('error') or {'code': 'interrupted' if self.terminal == 'interrupted'
                                             else 'provider_failed'}, outcome='unknown')
        elif t=='bridge_error':
            self.failed=True
            self.event('error',{'code':ev.get('code','provider_failed')})
        elif t=='assistant':
            for block in (ev.get('message') or {}).get('content',[]):
                if block.get('type')=='text':self.event('text_delta',{'text':block.get('text',''),'parent_id':ev.get('agent_id')})
        elif t=='tool_call':
            status=ev.get('status','unknown')
            self.tool(ev.get('call_id'),ev.get('name','tool'),ev.get('args'),done=status in SUCCESS|FAILURE,result=ev.get('result'),status=status,parent=ev.get('agent_id'))
            if ev.get('truncated'):self.event('gap',{'reason':'provider_truncated_tool','call_id':ev.get('call_id')})
        elif t=='task':
            self.task(ev.get('task_id'),ev.get('status','unknown'),parent_id=ev.get('agent_id'),description=ev.get('text'))
        elif t in {'usage','bridge_usage'}:
            self.event('usage',{'source':'cursor_sdk','scope':ev.get('scope','observation'),'tokens':ev.get('usage'),
                                'cost':ev.get('cost'),'cost_kind':'provider_reported'})
        elif t=='request':self.event('permission_required',{'request_id':ev.get('request_id')})
        elif t in {'status','system'}:self.event('status',{'status':ev.get('status') or ev.get('subtype')})
        elif t in {'thinking','user'}:return
        else:self.event('gap',{'reason':'unsupported_event','native_type':t})


def error_code(value):
    return normalize(None, value)['code']
