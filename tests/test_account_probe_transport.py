"""Bounded native account RPC reads, without credentials or model execution."""
import sys
import time

import pytest

from agentbridge.account_probe import CodexAppServerProbe
from agentbridge.errors import BridgeError
from agentbridge.models import Account


def _probe(tmp_path, script, timeout=1):
    provider = tmp_path / 'provider.py'
    provider.write_text(script)
    home = tmp_path / 'native-home'
    home.mkdir()
    return CodexAppServerProbe(Account('test', 'codex', home=str(home),
                              command=(sys.executable, str(provider))), timeout=timeout)


def test_partial_unterminated_json_cannot_block_past_rpc_timeout(tmp_path):
    probe = _probe(tmp_path, 'import sys,time\nfor line in sys.stdin:\n sys.stdout.write("{");sys.stdout.flush();time.sleep(10)\n',
                   timeout=0.2)
    started = time.monotonic()
    try:
        with pytest.raises(BridgeError) as caught:
            probe._rpc('initialize')
        assert caught.value.code == 'provider_timeout'
        assert time.monotonic() - started < 2
    finally:
        if probe.process:
            probe.process.kill()
            probe.process.wait(timeout=2)
        probe.close()


def test_notification_and_response_in_same_pipe_chunk_are_both_read(tmp_path):
    probe = _probe(tmp_path, '''import json,sys
for line in sys.stdin:
 request=json.loads(line)
 if 'id' not in request: continue
 result={'account':{'type':'chatgpt','email':'owner@example.test'}} if request['method']=='account/read' else {}
 notification=json.dumps({'method':'account/rateLimits/updated','params':{}})
 response=json.dumps({'id':request['id'],'result':result})
 sys.stdout.write(notification+'\\n'+response+'\\n');sys.stdout.flush()
''')
    observed = probe.read()
    assert observed['status'] == 'authenticated'
    assert observed['identity']['email'] == 'owner@example.test'
    assert probe.process is None
