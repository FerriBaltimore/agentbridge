import json,textwrap
from agentbridge import Account,Bridge,RunOptions

def test_worker_runs_fake_codex_and_persists_events(tmp_path,monkeypatch):
    fake=tmp_path/'fake-codex';fake.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json
        print(json.dumps({"type":"thread.started","thread_id":"native-1"}),flush=True)
        print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"hello"}}),flush=True)
        print(json.dumps({"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":1}}),flush=True)
    '''));fake.chmod(0o700)
    home=tmp_path/'home';home.mkdir();b=Bridge(tmp_path/'state');b.register(Account('fake','codex',home=str(home),command=(str(fake),),env_names=('FAKE_TOKEN',)));monkeypatch.setenv('FAKE_TOKEN','secret')
    session=b.session('fake',tmp_path);run=b.submit(session['id'],'hello',options=RunOptions(timeout=10));result=run.wait(10)
    assert result['state']=='completed' and run.text=='hello' and run.consumption['observations'][0]['tokens']['input_tokens']==3
    assert all('secret' not in json.dumps(e.data) for e in run.events())
