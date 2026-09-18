import json
from agentbridge import Account, Bridge, RunOptions

def setup(tmp_path):
    bridge=Bridge(tmp_path/'state');bridge.register(Account('fake','codex',home=str(tmp_path/'home')));return bridge

def test_idempotent_admission_and_sql_events(tmp_path):
    b=setup(tmp_path);session=b.session('fake',tmp_path)
    first,created=b.store.admit('run1',session['id'],'hello',RunOptions(),'same')
    second,created2=b.store.admit('run2',session['id'],'hello',RunOptions(),'same')
    assert (first,created)==('run1',True) and (second,created2)==('run1',False)
    assert [e.kind for e in b.store.events(run_id='run1')]==['user']

def test_context_marks_unknown_effect_and_keeps_archive(tmp_path):
    b=setup(tmp_path);session=b.session('fake',tmp_path);b.store.admit('run1',session['id'],'do thing',RunOptions(),'key');b.store.emit('run1','tool_call',{'call_id':'call','name':'shell','input':{'command':'touch x'}})
    bundle=b.export_context(session['id'],budget_bytes=10000);payload=json.loads(bundle.text.split('\n',1)[1])
    assert payload['unknown_outcomes'][0]['call_id']=='call' and bundle.archive_sha256 and (b.root/'archives').is_dir()

def test_context_refuses_when_mandatory_evidence_does_not_fit(tmp_path):
    b=setup(tmp_path);session=b.session('fake',tmp_path);b.store.admit('run1',session['id'],'x'*5000,RunOptions(),'key')
    try:b.export_context(session['id'],budget_bytes=2048)
    except Exception as error:assert getattr(error,'code',None)=='context_over_budget'
    else:raise AssertionError('expected context_over_budget')
