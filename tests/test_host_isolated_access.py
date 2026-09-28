"""Real Codex tools keep selected context behind a projected host boundary."""

from contextlib import contextmanager
import json
from pathlib import Path
import shlex
import socket
import socketserver
import sys
from threading import Thread
from uuid import uuid4

import pytest

from agentbridge import Account, Bridge
from agentbridge.errors import BridgeError
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from fixtures.test_responses_server import responses_server, function_events, tool_events
from test_bundled_native_continuity import pytestmark
from test_interactive_inputs import context_package


@contextmanager
def tools_server(path, operation):
    calls = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            request = json.loads(self.rfile.readline())
            assert request['operation_id'] == operation
            assert request['capability'] == 'FIXTURE_HOST_ISOLATED_CAPABILITY'
            calls.append(request['method'])
            result = ({'tools': [{'name': 'fixture_echo', 'description': 'Synthetic echo',
                                  'inputSchema': {'type': 'object', 'properties': {}}}]}
                      if request['method'] == 'tools/list' else
                      {'content': [{'type': 'text', 'text': 'SELECTED_TOOL_RESULT'}]})
            self.wfile.write((json.dumps({'result': result}) + '\n').encode())

    with socketserver.ThreadingUnixStreamServer(str(path), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield calls
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_host_isolated_shell_context_mcp_continuity_and_private_sockets(tmp_path, monkeypatch):
    # Fullbrain workers run the trusted system interpreter inside their outer namespace.
    monkeypatch.setattr(sys, 'executable', '/usr/bin/python3')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'secret').write_text('SYNTHETIC_SERVICE_SECRET')
    foreign = tmp_path / 'other-tenant'
    foreign.mkdir()
    (foreign / 'secret').write_text('SYNTHETIC_FOREIGN_SECRET')
    probe = workspace / 'probe.py'
    probe.write_text('''import json,os,socket,subprocess,sys,threading
from pathlib import Path
result={}
assert Path('/proc/self/exe').resolve() == Path('/usr/bin/python3').resolve()
t=threading.Thread(target=lambda:None);t.start();t.join()
for name,path in json.loads(sys.argv[1]).items():
 try:Path(path).read_text();result[name]=True
 except OSError:result[name]=False
for name,path in json.loads(sys.argv[2]).items():
 try:
  with socket.socket(socket.AF_UNIX) as c:c.connect(path)
  result[name]=True
 except OSError:result[name]=False
Path('native-result').write_text('NATIVE_WRITE')
child=subprocess.run(['/usr/bin/python3','-c',
 'from pathlib import Path; Path("child-result").write_text("CHILD_WRITE")'],check=True)
result['nested_namespace']=subprocess.run(['/usr/bin/unshare','-Ur','true'],
 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
print('HOST_ISOLATED='+json.dumps(result))
''')
    private = socket.socket(socket.AF_UNIX)
    private.bind(str(state / 'supervisor.sock'))
    private.listen()
    sibling = socket.socket(socket.AF_UNIX)
    sibling.bind(str(state / 'other-chat.sock'))
    sibling.listen()
    operation = str(uuid4())
    socket_path = tmp_path / 'tools.sock'
    package = context_package(tools=({'name': 'fixture_echo'},))
    descriptor = {'version': 1, 'socket_path': str(socket_path), 'operation_id': operation,
                  'capability': 'FIXTURE_HOST_ISOLATED_CAPABILITY'}
    command = shlex.join(['/usr/bin/python3', str(probe), json.dumps({
        'state': str(state / 'secret'), 'foreign': str(foreign / 'secret'),
        'host_proc': '/proc/1/environ'}), json.dumps({
        'supervisor': str(state / 'supervisor.sock'), 'other_chat': str(state / 'other-chat.sock')})])
    context_seen = []

    def select_events(request, observed):
        serialized = json.dumps(request)
        context_seen.append('selected fixture rule' in serialized
                            and 'The fixture source says blue.' in serialized)
        outputs = observed['tool_outputs']
        if not outputs:
            return tool_events(command, observed['tool_names'])
        if len(outputs) == 1:
            namespace = next(item for item in request['tools']
                             if item.get('name') == 'mcp__agentbridge_execution')
            assert namespace['tools'][0]['name'] == 'fixture_echo'
            return function_events('fixture_echo', {}, 'fixture-mcp-call', namespace['name'])
        return None

    bridge = Bridge(state)
    try:
        with tools_server(socket_path, operation) as calls, responses_server(
                'native', 'codex', ('fixture-model',), select_events=select_events) as (endpoint, seen):
            monkeypatch.setenv('FIXTURE_NATIVE_KEY', 'synthetic-client-key')
            monkeypatch.setenv('FIXTURE_NATIVE_MANAGEMENT', 'synthetic-management-key')
            seed_authenticated_proxy_account(bridge.store, Account(
                'native', 'codex', provider='codex', supported_models=('fixture-model',),
                proxy_base_url=endpoint, key_env='FIXTURE_NATIVE_KEY',
                management_key_env='FIXTURE_NATIVE_MANAGEMENT'), observe_local=True)
            instance = bridge.instance_create(model='fixture-model', workspace_path=workspace)
            options = {'sandbox_mode': 'danger-full-access', 'context_package': package,
                       'mcp': descriptor, 'timeout_ms': 30000}
            with pytest.raises(BridgeError, match='Full access'):
                bridge.message_create(instance['id'], 'fixture request baseline', **options)
            assert not bridge.runs()
            accepted = bridge.message_create(instance['id'], 'fixture request candidate',
                                             host_isolated=True, **options)
            run = bridge.run(accepted['turn_id'])
            assert run.wait(40)['state'] == 'completed', run.snapshot
            assert not [event.data for event in run.events() if event.kind == 'gap']
            assert (workspace / 'native-result').read_text() == 'NATIVE_WRITE'
            assert (workspace / 'child-result').read_text() == 'CHILD_WRITE'
            output = '\n'.join(str(value) for value in seen[-1]['tool_outputs'])
            assert 'HOST_ISOLATED=' in output
            for key in ('state', 'foreign', 'supervisor', 'other_chat', 'nested_namespace'):
                assert f'"{key}": false' in output, output
            assert all(context_seen)
            assert 'tools/call' in calls and 'SELECTED_TOOL_RESULT' in output, (seen, calls)
            native_id = bridge.instance_get(instance['id'])['native_session_id']
            resumed = bridge.message_create(instance['id'], 'fixture request continuation',
                                            host_isolated=True, **options)
            assert bridge.run(resumed['turn_id']).wait(40)['state'] == 'completed'
            assert not [event.data for event in bridge.run(resumed['turn_id']).events()
                        if event.kind == 'gap']
            assert bridge.instance_get(instance['id'])['native_session_id'] == native_id
            assert [text.split('UNTRUSTED SOURCE')[0] for text in seen[-1]['prompts']] == [
                'fixture request candidate', 'fixture request continuation']
    finally:
        bridge.close(cancel=True)
        private.close()
        sibling.close()
