"""Explicit cancellation cannot turn into an automatic retry."""

import textwrap
import time

from agentbridge import RunOptions
from fixtures.test_proxy_worker_fixture import MODEL, bridge_with_proxy, management_server


def test_stop_is_cancelled_and_does_not_retry(tmp_path, monkeypatch):
    fake = tmp_path / 'slow-codex'
    fake.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json,time
        print(json.dumps({"type":"thread.started","thread_id":"native-slow"}),flush=True)
        time.sleep(30)
    '''))
    fake.chmod(0o700)
    with management_server() as port:
        bridge = bridge_with_proxy(tmp_path, monkeypatch, port, command=(str(fake),))
        session = bridge.session('fixture', tmp_path, model=MODEL)
        run = bridge.submit(session['id'], 'wait', options=RunOptions(timeout=30, stop_grace=1))
        time.sleep(.2)
        result = run.stop(wait=True, timeout=10)
        assert result['state'] == 'cancelled'
        assert result['error'] == 'user_stop'
        assert not bridge.recover()['interrupted']
