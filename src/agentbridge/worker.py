"""Detached run owner. Pipes are drained while Stop and timeout are monitored."""
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time

from .models import Account, RunOptions
from .process import identity
from .protocols import Parser
from .security import Redactor, base_environment
from .store import Store
from .transports import command
from .credentials import CURSOR_KEY_ENV

MAX_LINE=8*1024*1024


def main():
    os.umask(0o077)
    root,run_id=sys.argv[1:3]
    store=Store(root)
    if not store.claim(run_id,os.getpid(),identity(os.getpid())):return
    run=store.get('runs',run_id)
    session=store.get('sessions',run['session_id'])
    account=Account(**json.loads(store.get('accounts',run['account_id'])['config']))
    options=RunOptions(**json.loads(run['options']))
    child=None
    parser=None
    stopped=[False]
    signal.signal(signal.SIGTERM,lambda *_:stopped.__setitem__(0,True))
    signal.signal(signal.SIGINT,lambda *_:stopped.__setitem__(0,True))
    try:
        # Values are passed over this private pipe, never stored or placed in argv.
        secrets=json.loads(sys.stdin.read())
        redactor=Redactor(secrets.values())
        emit=lambda kind,data:store.emit(run_id,kind,redactor.clean(data))
        parser=Parser(account.engine,emit)
        env=base_environment()
        env.update(secrets)
        env['PYTHONPATH']=os.path.dirname(os.path.dirname(__file__))
        if account.engine=='codex':env['CODEX_HOME']=account.home
        if account.engine=='claude':env['CLAUDE_CONFIG_DIR']=account.home
        cmd=command(account,session,options)
        prompt=run['prompt']
        if session.get('context') and not session.get('native_id'):
            prompt=session['context']+'\n\nCurrent user request:\n'+prompt
        if account.engine=='cursor':
            payload=json.dumps({'prompt':prompt,'cwd':session['cwd'],'model':options.model or session['model'],
                'native_id':session.get('native_id'),'key_env':account.key_env or CURSOR_KEY_ENV,
                'sandbox':options.sandbox,'tools':list(options.allowed_tools),'collect_usage':options.collect_usage})
        else:payload=prompt
        if run['stop_requested'] or stopped[0]:
            store.finish(run_id,'cancelled','user_stop');return
        child=subprocess.Popen(cmd,cwd=session['cwd'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,start_new_session=True)
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
        buffer=b'';stderr_bytes=0;start=time.monotonic();term_at=None;reason=None;last_tick=0
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
                    stderr_bytes+=len(data);continue # Raw provider error bodies may contain credentials.
                buffer+=data
                if len(buffer)>MAX_LINE and b'\n' not in buffer:
                    emit('gap',{'reason':'event_too_large'});reason=reason or 'protocol_error';buffer=b''
                while b'\n' in buffer:
                    line,buffer=buffer.split(b'\n',1)
                    if len(line)>MAX_LINE:
                        emit('gap',{'reason':'event_too_large'});reason=reason or 'protocol_error';continue
                    parse(line,parser)
            # Detached grandchildren holding a pipe cannot extend a completed run forever.
            if child.poll() is not None and not select.get_map():break
            if term_at is not None and now-term_at>options.stop_grace+2:break
        if buffer:parse(buffer,parser)
        code=child.wait(timeout=2)
        writer.join(timeout=2)
        unresolved=parser.end()
        if stderr_bytes:emit('diagnostic',{'stderr_bytes':stderr_bytes,'content_stored':False})
        if reason=='user_stop':state='cancelled'
        elif reason or code!=0 or parser.failed:state='failed'
        elif parser.terminal!='completed':state='interrupted';reason='missing_terminal_event'
        elif unresolved:state='incomplete';reason='unresolved_work'
        else:state='completed'
        store.finish(run_id,state,reason or ('provider_failed' if state=='failed' else None),code)
    except BaseException as error:
        if child and child.poll() is None:
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            child.wait(timeout=5)
        if parser:parser.end()
        # Exception bodies can embed env, prompts and provider responses.
        store.emit(run_id,'error',{'code':'worker_failed','exception_type':type(error).__name__})
        store.finish(run_id,'failed','worker_failed')


def parse(line, parser):
    if not line.strip():return
    try:event=json.loads(line)
    except (ValueError,UnicodeDecodeError):
        parser.event('gap',{'reason':'invalid_json'});return
    try:parser.feed(event)
    except (TypeError,ValueError,AttributeError,KeyError):
        parser.event('gap',{'reason':'malformed_provider_event'})


if __name__=='__main__':main()
