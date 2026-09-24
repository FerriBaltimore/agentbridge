"""The Codex worker persists events through a verified local proxy route."""

import json
import textwrap

from agentbridge import RunOptions
from fixtures.test_proxy_worker_fixture import MODEL, bridge_with_proxy, management_server


def test_worker_runs_fake_codex_and_persists_events(tmp_path, monkeypatch):
    fake = tmp_path.parent / f'{tmp_path.name}-fake-codex'
    fake.write_text(textwrap.dedent('''\
        #!/usr/bin/python3
        import json
        print(json.dumps({"type":"thread.started","thread_id":"native-1"}), flush=True)
        print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"hello"}}), flush=True)
        print(json.dumps({"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":1}}), flush=True)
    '''))
    fake.chmod(0o700)
    with management_server() as port:
        bridge = bridge_with_proxy(tmp_path, monkeypatch, port, command=(str(fake),))
        session = bridge.session('fixture', tmp_path, model=MODEL)
        run = bridge.submit(session['id'], 'hello', options=RunOptions(timeout=10))
        result = run.wait(10)
        assert result['state'] == 'completed'
        assert run.text == 'hello'
        assert run.consumption['observations'][0]['tokens']['input_tokens'] == 3
        assert all('fixture-client-key' not in json.dumps(e.data) for e in run.events())
