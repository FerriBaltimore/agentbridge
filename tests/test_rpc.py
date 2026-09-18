import io,json
from agentbridge.cli import rpc
from agentbridge import Bridge

def test_json_rpc_capabilities_and_errors(tmp_path):
    input_data=''.join(json.dumps(x)+'\n' for x in [{'jsonrpc':'2.0','id':1,'method':'capabilities','params':{}},{'jsonrpc':'2.0','id':2,'method':'unknown','params':{}}])
    out=io.StringIO();rpc(Bridge(tmp_path),io.StringIO(input_data),out);responses=[json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[0]['result']['codex']['native_transfer'] is True
    assert responses[1]['error']['data']['code']=='method_not_found'
