"""Detached run owner. Pipes are drained while Stop and timeout are monitored."""
import json
import os
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .models import Account, RunOptions
from .process import identity
from .protocols import Parser
from .security import Redactor, base_environment
from .store import Store
from .transports import command, duplex, require_proxy_account
from .attachments import text_prompt
from dataclasses import asdict
from .execution_outcome import finish
from .provider_errors import normalize
from .error_observer import ErrorObserver
from .errors import BridgeError
from .provider_contracts import ContractRegistry
from .accounts import AccountService
from .routing.admission import verify_proxy_model
from .routing.service import RoutingService
from .native_sandbox import wrap
from .execution_context import (MCP_CAPABILITY_ENV, PRIVATE_EXECUTION_KEY,
                                mcp_environment, verify)

MAX_LINE=8*1024*1024


def main():
    os.umask(0o077)
    root,run_id=sys.argv[1:3]
    store=None
    claimed=False
    account=None
    child=None
    native_temp=None
    parser=None
    writer=None
    select=None
    stopped=[False]
    signal.signal(signal.SIGTERM,lambda *_:stopped.__setitem__(0,True))
    signal.signal(signal.SIGINT,lambda *_:stopped.__setitem__(0,True))
    try:
        store=Store(root)
        claimed=store.claim(run_id,os.getpid(),identity(os.getpid()))
        if not claimed:return
        run=store.get('runs',run_id)
        session=store.get('sessions',run['session_id'])
        account=Account(**json.loads(store.get('accounts',run['account_id'])['config']))
        options=RunOptions(**json.loads(run['options']))
        require_proxy_account(account)
        # The management key arrives on the private pipe and is removed before
        # any provider environment or duplex payload is assembled.
        private=json.loads(sys.stdin.read())
        if (isinstance(private, dict) and set(private) == {
                PRIVATE_EXECUTION_KEY, 'secrets', 'execution'}
                and private[PRIVATE_EXECUTION_KEY] is True):
            secrets, execution = private['secrets'], verify(options, private['execution'])
        else:
            secrets, execution = private, None
            if options.context_package_digest or options.mcp_binding_digest:
                raise BridgeError('context_required', 'Fresh execution context is required for this turn.')
        management_name=account.management_key_env
        management_key=secrets.pop(management_name, None)
        if not management_key:
            raise BridgeError('credential_unavailable', 'The proxy management credential is unavailable.')
        previous_management_key=os.environ.get(management_name)
        os.environ[management_name]=management_key
        try:
            verify_proxy_model(RoutingService(store, AccountService(store)), account,
                               options.model or session['model'], refresh=True)
        finally:
            if previous_management_key is None:
                os.environ.pop(management_name, None)
            else:
                os.environ[management_name]=previous_management_key
        ContractRegistry(store).verify_run(account,run_id)
        mcp_env = mcp_environment(execution['mcp']) if execution else {}
        redactor=Redactor((*secrets.values(), management_key, mcp_env.get(MCP_CAPABILITY_ENV, '')))
        emit=lambda kind,data:store.emit(run_id,kind,redactor.clean(data))
        observe_error = ErrorObserver(store, account, run_id)
        parser=Parser(account.engine,emit,error_handler=observe_error)
        env=base_environment()
        env.update(secrets)
        env.update(mcp_env)
        env['PYTHONPATH']=os.path.dirname(os.path.dirname(__file__))
        from .proxy import session_home
        env['CODEX_HOME']=str(session_home(store.root,session['id']))
        env['HOME']=env['CODEX_HOME']
        cmd=command(account,session,options,state_root=store.root)
        prompt=run['prompt']
        if session.get('context') and not session.get('native_id'):
            prompt=session['context']+'\n\nCurrent user request:\n'+prompt
        prompt = text_prompt(prompt, options.attachments)
        if duplex(account, options):
            payload = json.dumps({'engine': account.engine, 'prompt': prompt, 'cwd': session['cwd'],
                'model': options.model or session.get('model'), 'native_id': session.get('native_id'),
                'options': asdict(options), 'root': str(store.root), 'turn_id': run_id,
                'command': command(account, session, options, native_transport=True,
                                   state_root=store.root),
                'secret_names': [*secrets, *([MCP_CAPABILITY_ENV] if mcp_env else [])],
                'context_package': execution['context_package'] if execution else None,
                'mcp_enabled': bool(mcp_env)})
        else:
            payload=prompt
            native_temp=tempfile.mkdtemp(prefix='agentbridge-native-')
            env['TMPDIR']=native_temp
            cmd=wrap(cmd, workspace_write=options.sandbox != 'read-only')
        if run['stop_requested'] or stopped[0]:
            store.finish(run_id,'cancelled','user_stop');return
        try:
            child=subprocess.Popen(cmd,cwd=session['cwd'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,start_new_session=True)
        except OSError:
            raise BridgeError('provider_unavailable', 'The native provider could not be started.',
                              phase='launch', outcome='not_started') from None
        store.update(run_id,child_pid=child.pid,child_identity=identity(child.pid),updated=time.time())
        emit('run_started',{'engine':account.engine,'account_id':account.id})
        writer_error=[]
        def write_input():
            try:
                child.stdin.write(payload.encode());child.stdin.close()
            except (BrokenPipeError,OSError) as e:writer_error.append(type(e).__name__)
        writer=threading.Thread(target=write_input,daemon=True);writer.start()
        select=selectors.DefaultSelector()
        for pipe in (child.stdout,child.stderr):
            os.set_blocking(pipe.fileno(),False);select.register(pipe,selectors.EVENT_READ)
        buffer=b'';stderr_bytes=0;stderr_tail=b'';start=time.monotonic();term_at=None;reason=None;last_tick=0
        while select.get_map() or child.poll() is None:
            now=time.monotonic()
            if now-last_tick>=0.1:
                control=store.get('runs',run_id)
                if (control['stop_requested'] or stopped[0]) and not reason:reason='user_stop'
                if now-start>=options.timeout and not reason:reason='timeout'
                if reason and term_at is None:
                    term_at=now
                    try:os.killpg(child.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                if term_at is not None and now-term_at>=options.stop_grace:
                    try:os.killpg(child.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                store.update(run_id,updated=time.time())
                last_tick=now
            for key,_ in select.select(0.05):
                try:data=os.read(key.fileobj.fileno(),65536)
                except BlockingIOError:continue
                if not data:
                    select.unregister(key.fileobj);key.fileobj.close();continue
                if key.fileobj is child.stderr:
                    stderr_bytes += len(data)
                    # Classify in memory only. Never store raw diagnostics, headers or credentials.
                    stderr_tail = (stderr_tail + data)[-16384:]
                    continue
                buffer+=data
                if len(buffer)>MAX_LINE and b'\n' not in buffer:
                    emit('gap',{'reason':'event_too_large'});reason=reason or 'protocol_error';buffer=b''
                while b'\n' in buffer:
                    line,buffer=buffer.split(b'\n',1)
                    if len(line)>MAX_LINE:
                        emit('gap',{'reason':'event_too_large'});reason=reason or 'protocol_error';continue
                    parse(line,parser)
            # The wrapper owns this run's process group. No background lifetime
            # is promised after it exits; close descendants before draining EOF.
            if child.poll() is not None:
                try:os.killpg(child.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                if not select.get_map():break
            if term_at is not None and now-term_at>options.stop_grace+2:break
        if buffer:parse(buffer,parser)
        code=child.wait(timeout=2)
        # Also cover the case where every pipe closed before wrapper exit.
        try:os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:pass
        writer.join(timeout=2)
        if writer_error:reason=reason or 'input_delivery_failed'
        unresolved=parser.end()
        if stderr_bytes:emit('diagnostic',{'stderr_bytes':stderr_bytes,'content_stored':False})
        state, failure, issue = finish(parser, reason=reason, exit_code=code, unresolved=unresolved,
                                       stderr=stderr_tail.decode('utf-8', errors='replace'))
        if issue:
            issue = observe_error(issue)
            failure = issue['code']
            emit('error', issue)
        store.finish(run_id, state, failure, code)
    except BaseException as error:
        # Each recovery step is independent: a second storage failure must not
        # skip cleanup or a terminal commit that could still succeed.
        cleaned=True
        if child:
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            except OSError:cleaned=False
            try:child.wait(timeout=5)
            except (OSError,subprocess.TimeoutExpired):cleaned=False
        if parser:
            try:parser.end()
            except Exception:pass
        # Exception bodies can embed env, prompts and provider responses.
        code=error.code if isinstance(error,BridgeError) else 'worker_failed'
        issue=normalize(account.engine if account else None, {'code': code},
                        phase='execution' if child else 'launch',
                        outcome='unknown' if child else 'not_started')
        if isinstance(error,BridgeError) and code in {
                'provider_contract_unverified','provider_contract_changed','provider_contract_invalid',
                'invalid_proxy_account','proxy_binding_unverified','model_required','model_unavailable',
                'credential_unavailable'}:
            issue={**error.safe_data(),'engine':account.engine if account else None,
                   'terminal':True,'provider_retrying':False}
        if claimed:
            try:store.emit(run_id,'error',issue)
            except Exception:pass
            if cleaned:
                try:store.finish(run_id,'interrupted' if child else 'failed',issue['code'])
                except Exception:pass
        # Persistent disk failure cannot be acknowledged as a saved result.
        # A dead owner remains reconcilable once the store becomes writable.
        raise SystemExit(1) from None
    finally:
        if select is not None:
            try:select.close()
            except OSError:pass
        if writer is not None:
            try:writer.join(timeout=2)
            except RuntimeError:pass
        if child is not None and child.poll() is not None:
            for pipe in (child.stdin,child.stdout,child.stderr):
                try:pipe.close()
                except OSError:pass
        if native_temp is not None and (child is None or child.poll() is not None):
            shutil.rmtree(native_temp, ignore_errors=True)


def parse(line, parser):
    if not line.strip():return
    try:event=json.loads(line)
    except (ValueError,UnicodeDecodeError):
        parser.event('gap',{'reason':'invalid_json'})
        parser.error({'code':'provider_protocol_error'},outcome='unknown');return
    try:parser.feed(event)
    except (TypeError,ValueError,AttributeError,KeyError):
        parser.event('gap',{'reason':'malformed_provider_event'})
        parser.error({'code':'provider_protocol_error'},outcome='unknown')


if __name__=='__main__':main()
