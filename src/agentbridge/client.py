"""Public SDK. No server, event loop or Fullbrain installation required."""
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from .continuity import all_events, build, unresolved
from .errors import BridgeError, BusyError, UnsupportedError
from .models import Account, CAPABILITIES, RunOptions, TERMINAL, identifier
from .native import copy_session
from .process import alive
from .security import Redactor, base_environment
from .store import Store, dumps
from .transports import command
from . import usage
from .accounts import AccountService


class Bridge:
    def __init__(self, root='.agentbridge'):
        if os.name!='posix':raise UnsupportedError('Process supervision currently requires a POSIX host.')
        self.store=Store(root)
        self.account_service=AccountService(self.store)
        self._children={}

    @property
    def root(self):return self.store.root

    def capabilities(self, engine=None):
        if engine is None:return {k:asdict(v) for k,v in CAPABILITIES.items()}
        if engine not in CAPABILITIES:raise BridgeError('invalid_engine','Unknown engine.')
        return asdict(CAPABILITIES[engine])

    def register(self, account: Account):
        return self.account_service.register(account)

    def accounts(self):
        return self.account_service.list()

    def account(self, id):
        return self.account_service.get(id)

    def account_status(self, account_id, *, refresh=False):
        return self.account_service.status(account_id, refresh=refresh)

    def account_usage(self, account_id, *, refresh=False):
        return self.account_service.usage(account_id, refresh=refresh)

    def account_usage_history(self, account_id, *, limit=100):
        return self.account_service.history(account_id, limit=limit)

    def session(self, account_id, cwd, *, model=None):
        account=self.account(account_id)
        cwd=str(Path(cwd).expanduser().resolve())
        if not Path(cwd).is_dir():raise BridgeError('invalid_workspace','Workspace must be an existing directory.')
        if account.engine=='cursor' and not model:raise BridgeError('model_required','Cursor requires an explicit model for reliable resume.')
        id=uuid4().hex
        self.store.add_session(id,account_id,cwd,model)
        return self.get_session(id)

    def get_session(self, id):return self.store.get('sessions',identifier(id))
    def sessions(self):return self.store.list('sessions')
    def runs(self):return self.store.list('runs')
    def run(self, id):
        self.store.get('runs',identifier(id))
        return Run(self,id)

    def submit(self, session_id, prompt, *, options=None, request_key=None):
        options=options or RunOptions()
        if not isinstance(prompt,str) or not prompt.strip():raise BridgeError('empty_prompt','A nonempty prompt is required.')
        session=self.get_session(session_id)
        account=self.account(session['account_id'])
        command(account,session,options) # Refuse unsupported controls before recording a request.
        secrets={}
        for name in (*account.env_names,*((account.key_env,) if account.key_env else ())):
            if name not in os.environ:raise BridgeError('credential_unavailable',f'Required environment variable {name} is unavailable.')
            secrets[name]=os.environ[name]
        prompt=Redactor(secrets.values()).clean(prompt)
        id,created=self.store.admit(uuid4().hex,session_id,prompt,options,request_key)
        if not created:return Run(self,id)
        env=base_environment()
        env['PYTHONPATH']=str(Path(__file__).resolve().parent.parent)
        try:
            proc=subprocess.Popen([sys.executable,'-m','agentbridge.worker',str(self.root),id],env=env,
                stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            self._children[id]=proc
            proc.stdin.write(dumps(secrets).encode());proc.stdin.close()
        except (OSError,BrokenPipeError):
            self.store.finish(id,'failed','launch_failed')
            raise BridgeError('launch_failed','Could not start the AgentBridge worker.') from None
        return Run(self,id)

    def export_context(self, session_id, *, budget_bytes=128000):
        self.get_session(session_id)
        return build(self.store,session_id,budget_bytes)

    def transfer(self, session_id, account_id, *, model=None, mode='auto', budget_bytes=128000):
        """Create a new session branch; the source remains independently resumable.

        auto: same-engine native if proven locally, otherwise bounded portable context.
        native: fail rather than silently use portable context.
        portable: always start a fresh provider session with a public evidence bundle.
        """
        if mode not in ('auto','native','portable'):raise BridgeError('invalid_mode','Choose auto, native or portable.')
        source=self.get_session(session_id);old=self.account(source['account_id']);target=self.account(account_id)
        if target.id==old.id:raise BridgeError('same_account','Continue the existing session on the same account.')
        target_model=model if model is not None else source['model'] if old.engine==target.engine else None
        if target.engine=='cursor' and not target_model:raise BridgeError('model_required','Specify the destination Cursor model.')
        # Serialize transfer with run admission. This protects sessions managed by this store.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active=db.execute("SELECT 1 FROM runs WHERE account_id IN (?,?) AND state IN ('starting','running','stopping')",
                              (old.id,target.id)).fetchone()
            if active:raise BusyError()
            native_id=None;fallback=None
            if mode!='portable' and old.engine==target.engine and source['native_id'] and old.home and target.home:
                try:native_id=copy_session(old.engine,source['native_id'],old.home,target.home,list(target.command or (target.engine,)))
                except (BridgeError,OSError) as error:
                    if mode=='native':raise
                    fallback=getattr(error,'code','native_unavailable')
            elif mode=='native':raise UnsupportedError('Native transfer requires the same supported engine and an observed native session.')
            bundle=None if native_id else self.export_context(session_id,budget_bytes=budget_bytes)
            # Native copy also carries uncertain side effects which need verification.
            unknown=unresolved(list(all_events(self.store,session_id)))
            context=bundle.text if bundle else ('Verify these unknown historical outcomes before repeating actions: '+dumps(unknown) if unknown else None)
            id=uuid4().hex
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
                       (id,target.id,source['cwd'],target_model,native_id,session_id,context,time.time()))
        return {**self.get_session(id),'transfer_mode':'native' if native_id else 'portable','fallback_reason':fallback,
                'context_omissions':len(bundle.omitted) if bundle else 0}

    def quota(self, account_id, *, allow_network=False, oauth_env=None):
        token=os.environ.get(oauth_env) if oauth_env else None
        return usage.snapshot(self.account(account_id),oauth_token=token,allow_network=allow_network)

    def recover(self):
        """Reattach live workers. Never repeat an interrupted request automatically."""
        report={'live':[],'interrupted':[],'unresolved':[]}
        for row in self.runs():
            if row['state'] in TERMINAL:continue
            id=row['id']
            if alive(row['worker_pid'],row['worker_identity']):report['live'].append(id);continue
            if row['state']=='starting' and time.time()-row['created']<15:
                report['unresolved'].append(id);continue
            if alive(row['child_pid'],row['child_identity']):
                report['unresolved'].append(id);continue # Never rerun work while its process may still execute.
            self.store.emit(id,'recovery',{'reason':'worker_lost','automatic_retry':False})
            pending=unresolved(self.run(id).events())
            for item in pending:
                kind='subagent' if item.get('kind')=='subagent' else 'tool_result'
                self.store.emit(id,kind,{**item,'outcome':'unknown','status':'unknown','reason':'worker_lost'})
            self.store.finish(id,'interrupted','worker_lost')
            report['interrupted'].append(id)
        return report

    def close(self, *, cancel=False):
        """Default detaches; cancel=True waits for runs owned by this Bridge instance."""
        for id,proc in list(self._children.items()):
            if cancel and self.run(id).status not in TERMINAL:self.run(id).stop(wait=True)
            if proc.poll() is not None:proc.wait();self._children.pop(id,None)

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


class Run:
    def __init__(self, bridge,id):self.bridge,self.id=bridge,id
    @property
    def snapshot(self):return self.bridge.store.get('runs',self.id)
    @property
    def status(self):return self.snapshot['state']
    @property
    def session(self):return self.bridge.get_session(self.snapshot['session_id'])
    @property
    def text(self):
        events=list(self.events())
        finals=[e.data.get('text','') for e in events if e.kind=='assistant' and not e.data.get('parent_id')]
        if finals:return '\n'.join(finals)
        return ''.join(e.data.get('text','') for e in events if e.kind=='text_delta')
    @property
    def consumption(self):return usage.summarize(self.events())
    @property
    def subagents(self):
        agents={}
        for e in self.events():
            if e.kind=='subagent':agents[e.data['agent_id']]={**e.data,'observed_at':e.at}
        return {'coverage':'partial','agents':list(agents.values())}

    def events(self, *, after=0, follow=False, timeout=None):
        start=time.monotonic()
        while True:
            batch=self.bridge.store.events(run_id=self.id,after=after)
            for event in batch:
                after=event.seq;yield event
            if batch:continue
            if not follow:return
            if self.status in TERMINAL:
                # Terminal state and final event commit together; re-read once after observing it.
                for event in self.bridge.store.events(run_id=self.id,after=after):yield event
                return
            if timeout is not None and time.monotonic()-start>=timeout:raise TimeoutError('Event wait timed out; the run remains active.')
            time.sleep(0.05)

    async def aevents(self, *, after=0, timeout=None):
        start=time.monotonic()
        while True:
            batch=await asyncio.to_thread(lambda:self.bridge.store.events(run_id=self.id,after=after))
            for event in batch:after=event.seq;yield event
            if batch:continue
            if await asyncio.to_thread(lambda:self.status in TERMINAL):
                for event in await asyncio.to_thread(lambda:self.bridge.store.events(run_id=self.id,after=after)):yield event
                return
            if timeout is not None and time.monotonic()-start>=timeout:raise TimeoutError('Event wait timed out; the run remains active.')
            await asyncio.sleep(0.05)

    def wait(self, timeout=None):
        start=time.monotonic()
        while self.status not in TERMINAL:
            if timeout is not None and time.monotonic()-start>=timeout:raise TimeoutError('Wait timed out; the run remains active.')
            self.bridge.recover()
            time.sleep(0.05)
        self.bridge.close()
        return self.snapshot

    async def await_result(self, timeout=None):return await asyncio.to_thread(self.wait,timeout)

    def stop(self, *, wait=False, timeout=15):
        self.bridge.store.stop(self.id)
        return self.wait(timeout) if wait else self.snapshot

    def resume(self, prompt=None, *, options=None, request_key=None):
        if self.status not in TERMINAL:raise BusyError()
        # Repeating the original prompt is deliberate and accompanied by uncertainty context.
        row=self.snapshot
        pending=unresolved(list(self.events()))
        if prompt is None:
            prompt='Continue the interrupted work. Verify effects before repeating them. Original request:\n'+row['prompt']
        if pending:prompt+='\nUnknown previous outcomes:\n'+dumps(pending)
        if not self.session['native_id']:
            bundle=self.bridge.export_context(row['session_id'])
            prompt=bundle.text+'\n\n'+prompt
        return self.bridge.submit(row['session_id'],prompt,options=options or RunOptions(**json.loads(row['options'])),request_key=request_key)
