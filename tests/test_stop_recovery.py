import json,textwrap,time
from agentbridge import Account,Bridge,RunOptions

def test_stop_is_cancelled_and_does_not_retry(tmp_path,monkeypatch):
    fake=tmp_path/'slow-codex';fake.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json,time
        print(json.dumps({"type":"thread.started","thread_id":"native-slow"}),flush=True)
        time.sleep(30)
    '''));fake.chmod(0o700)
    home=tmp_path/'home';home.mkdir();b=Bridge(tmp_path/'state');b.register(Account('fake','codex',home=str(home),command=(str(fake),),env_names=('FAKE_TOKEN',)));monkeypatch.setenv('FAKE_TOKEN','secret')
    session=b.session('fake',tmp_path);run=b.submit(session['id'],'wait',options=RunOptions(timeout=30,stop_grace=1));time.sleep(.2)
    result=run.stop(wait=True,timeout=10)
    assert result['state']=='cancelled'
    assert result['error']=='user_stop'
    assert not b.recover()['interrupted']
