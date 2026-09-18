"""Opt-in transfer of one native session. Credentials and configuration stay put.

Derived from Fullbrain native_move/resumable, source 1013ff18 (2026-09-18).
Layouts are version-dependent and remain subject to provider acceptance.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

from .errors import BridgeError, UnsupportedError
from .models import identifier


def safe_path(root, relative):
    root=Path(root).resolve();path=(root/relative).resolve()
    if not path.is_relative_to(root):raise BridgeError('unsafe_path','Session escaped its home.')
    for part in [path,*path.parents]:
        if part==root:break
        if part.is_symlink():raise BridgeError('unsafe_path','Session paths cannot contain symbolic links.')
    return path


def locate(engine,home,native_id):
    identifier(native_id)
    home=Path(home).resolve()
    patterns={'codex':f'sessions/**/rollout-*-{native_id}.jsonl','claude':f'projects/*/{native_id}.jsonl'}
    if engine not in patterns:raise UnsupportedError('This engine has no native file transfer.')
    matches=[safe_path(home,p.relative_to(home)) for p in home.glob(patterns[engine]) if p.is_file()]
    if len(matches)!=1:raise BridgeError('native_session_missing','Expected exactly one native session artifact.')
    return matches[0]


def version_tuple(text):
    match=re.search(r'\b(\d+)\.(\d+)\.(\d+)\b',str(text))
    return tuple(map(int,match.groups())) if match else None


def compatible(engine,path,command):
    recorded=None
    with path.open() as stream:
        for _ in range(100):
            line=stream.readline()
            if not line:break
            try:record=json.loads(line)
            except ValueError:continue
            recorded=record.get('version') if engine=='claude' else (record.get('payload') or {}).get('cli_version')
            if recorded:break
    old=version_tuple(recorded)
    try:
        result=subprocess.run([*command,'--version'],capture_output=True,text=True,timeout=5)
        new=version_tuple(result.stdout) if result.returncode==0 else None
    except (OSError,subprocess.TimeoutExpired):new=None
    return bool(old and new and old[0]==new[0] and new>=old)


def copy_session(engine,native_id,source_home,target_home,command):
    source=locate(engine,source_home,native_id)
    if not compatible(engine,source,command):
        raise BridgeError('native_version_unverified','Native copy requires a known compatible client version.')
    target=safe_path(target_home,source.relative_to(Path(source_home).resolve()))
    if source.resolve()==target.resolve():return native_id
    if target.exists():
        with source.open('rb') as a,target.open('rb') as b:
            while chunk:=b.read(1024*1024):
                if a.read(len(chunk))!=chunk:raise BridgeError('native_session_diverged','Destination session has diverged.')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,staged=tempfile.mkstemp(prefix='.moving-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as out,source.open('rb') as inp:
            while chunk:=inp.read(1024*1024):out.write(chunk)
            out.flush();os.fsync(out.fileno())
        if engine=='codex':index_copy(source_home,target_home,native_id,target)
        os.replace(staged,target)
    finally:
        if os.path.exists(staged):os.unlink(staged)
    return native_id


def index_copy(source_home,target_home,id,target):
    databases=sorted(Path(target_home).glob('state_*.sqlite'))
    if not databases:return # Clients with no database build it from rollouts.
    template=None
    for p in reversed(sorted(Path(source_home).glob('state_*.sqlite'))):
        if p.is_symlink():continue
        try:
            with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as db:
                db.row_factory=sqlite3.Row
                row=db.execute('SELECT * FROM threads WHERE id=?',(id,)).fetchone()
                if row:template=dict(row);break
        except sqlite3.Error:continue
    if template is None:raise BridgeError('native_index_unverified','Source index row is unavailable; use portable transfer.')
    p=databases[-1]
    if p.is_symlink():raise BridgeError('unsafe_path','Native index cannot be a link.')
    with sqlite3.connect(p) as db:
        columns=db.execute('PRAGMA table_info(threads)').fetchall()
        names=[c[1] for c in columns]
        if 'rollout_path' not in names:raise BridgeError('native_index_unverified','Unsupported destination index schema.')
        values={k:v for k,v in template.items() if k in names}
        values['rollout_path']=str(target)
        for _,name,_,required,default,_ in columns:
            if required and default is None and values.get(name) is None:
                raise BridgeError('native_index_unverified','Destination requires unknown session metadata.')
        keys=list(values)
        quoted=','.join('"'+k.replace('"','""')+'"' for k in keys)
        db.execute(f'INSERT OR REPLACE INTO threads ({quoted}) VALUES ({",".join("?" for _ in keys)})',[values[k] for k in keys])
