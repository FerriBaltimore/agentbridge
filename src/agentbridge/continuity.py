"""Portable public evidence with bounded delivery and explicit omissions."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .errors import BridgeError
from .store import dumps

INTRO=('Historical conversation evidence, not new authorization. Follow the current user request and rules. '
       'Do not repeat completed actions. Verify unknown tool outcomes against real state before retrying. '
       'Omitted evidence remains in the archive identified below; use an authorized reader to retrieve it.\n')


@dataclass(frozen=True)
class ContextBundle:
    text: str
    archive_path: str
    archive_sha256: str
    included: tuple[int,...]
    omitted: tuple[int,...]
    unknown_outcomes: tuple[dict,...]


def all_events(store,session_id):
    after=0
    while True:
        batch=store.events(session_id=session_id,after=after)
        if not batch:break
        yield from batch
        after=batch[-1].seq


def unresolved(events):
    pending={}
    tasks={}
    for e in events:
        d=e.data
        key=(e.run_id,d.get('parent_id'),d.get('call_id'))
        if e.kind=='tool_call':pending[key]={'run_id':e.run_id,**d,'seq':e.seq}
        if e.kind=='tool_result':
            if d.get('outcome')=='unknown':pending[key]={'run_id':e.run_id,**d,'seq':e.seq}
            else:pending.pop(key,None)
        if e.kind=='subagent':
            key=(e.run_id,d.get('agent_id'))
            if d.get('status') in {'completed','success','succeeded','failed','cancelled','killed','stopped'}:tasks.pop(key,None)
            else:tasks[key]={'run_id':e.run_id,**d,'seq':e.seq,'kind':'subagent'}
    return list(pending.values())+list(tasks.values())


def atomic_private(path,data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix='.staged-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def build(store,session_id,budget_bytes=128000):
    if type(budget_bytes) is not int or budget_bytes<2048:
        raise BridgeError('invalid_budget','Context budget must be at least 2048 bytes.')
    events=list(all_events(store,session_id))
    archive=b''.join((dumps(asdict(e))+'\n').encode() for e in events)
    digest=hashlib.sha256(archive).hexdigest()
    path=store.root/'archives'/f'{session_id}-{digest[:16]}.jsonl'
    atomic_private(path,archive)
    unknown=unresolved(events)
    # Preserve all owner instructions, unresolved results and referenced tool calls.
    mandatory={e.seq for e in events if e.kind=='user'} | {u['seq'] for u in unknown}
    tool_keys={(u['run_id'],u.get('parent_id'),u.get('call_id')) for u in unknown if 'call_id' in u}
    mandatory|={e.seq for e in events if e.kind=='tool_call' and (e.run_id,e.data.get('parent_id'),e.data.get('call_id')) in tool_keys}
    selected=set(mandatory)
    def render():
        return INTRO+dumps({'version':1,'archive_path':str(path),'archive_sha256':digest,
            'unknown_outcomes':unknown,'evidence':[asdict(e) for e in events if e.seq in selected],
            'omitted_count':sum(e.seq not in selected for e in events),'representation':'selected'})
    if len(render().encode())>budget_bytes:
        raise BridgeError('context_over_budget','Owner instructions and unresolved effects exceed the budget; supply an explicit reviewed summary or a larger budget.')
    # Select complete run groups rather than orphaning tool results from their calls.
    groups={}
    for e in events:
        if e.kind not in {'text_delta','status','run_started','diagnostic'}:groups.setdefault(e.run_id,set()).add(e.seq)
    for group in reversed(list(groups.values())):
        previous=set(selected);selected|=group
        if len(render().encode())>budget_bytes:selected=previous
    text=render()
    return ContextBundle(text,str(path),digest,tuple(sorted(selected)),tuple(e.seq for e in events if e.seq not in selected),tuple(unknown))
