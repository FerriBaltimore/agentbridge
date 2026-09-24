from agentbridge.event_contract import public_event
from agentbridge.models import Event
from agentbridge.protocols import Parser

def collect(engine,events):
    out=[];p=Parser(engine,lambda k,d:out.append((k,d)))
    for event in events:p.feed(event)
    return p,out

def test_codex_pairs_tools_and_usage():
    p,out=collect('codex',[{'type':'thread.started','thread_id':'thread-1'},{'type':'item.started','item':{'id':'call-1','type':'command_execution','command':'true'}},{'type':'item.completed','item':{'id':'call-1','type':'command_execution','status':'completed','exit_code':0,'aggregated_output':'ok'}},{'type':'turn.completed','usage':{'input_tokens':4,'output_tokens':2}}])
    assert ('session',{'native_id':'thread-1'}) in out
    assert [x[0] for x in out].count('tool_call')==1
    assert out[-1][0]=='usage' and out[-1][1]['tokens']['input_tokens']==4
    assert p.terminal=='completed' and not p.end()

def test_unknown_tool_result_is_explicit():
    p,out=collect('claude',[{'type':'assistant','message':{'content':[{'type':'tool_use','id':'t-1','name':'Bash','input':{'command':'x'}}]}}])
    assert p.end()
    assert out[-1][0]=='tool_result' and out[-1][1]['outcome']=='unknown'


def test_codex_compaction_has_start_and_completion_observations():
    _, emitted = collect('codex', [
        {'type': 'item.started', 'item': {'id': 'compact-1', 'type': 'context_compaction'}},
        {'type': 'item.completed', 'item': {'id': 'compact-1', 'type': 'context_compaction'}},
    ])
    assert [kind for kind, _ in emitted] == ['compaction_started', 'compaction']
    public = [public_event(Event(index, 'turn', 'instance', kind, 1.0, data), 'codex')
              for index, (kind, data) in enumerate(emitted, 1)]
    assert [event['kind'] for event in public] == ['context.compacting', 'context.compacted']
