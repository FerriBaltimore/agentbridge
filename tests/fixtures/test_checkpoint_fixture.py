"""Private durable Store fixtures; all credentials and native metadata are synthetic."""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from agentbridge import Bridge, RunOptions
from agentbridge.native_sessions import mark_native_launch
from fixtures.test_proxy_account_fixture import proxy_account, seed_authenticated_proxy_account


def prepared(tmp_path, *, durable=True):
    bridge = Bridge(tmp_path / 'state', owner_ref='fixture-owner', durable=durable)
    fixture = tmp_path / 'codex-fixture'
    source = Path(__file__).with_name('test_durable_codex.py')
    fixture.write_text('#!/usr/bin/env python3\n' + source.read_text())
    fixture.chmod(0o700)
    account = replace(proxy_account('fixture', 18371), command=(str(fixture),))
    seed_authenticated_proxy_account(bridge.store, account)
    bridge.store.add_session('instance', 'fixture', str(tmp_path), 'fixture-model')
    return bridge


def execution(bridge, *, instance='instance', turn='turn-1', payload_bytes=0):
    native_id = str(uuid4())
    bridge.store.admit(turn, instance, 'hello', RunOptions(model='fixture-model'), turn)
    mark_native_launch(bridge.store, turn)
    bridge.store.emit(turn, 'session', {'native_id': native_id})
    home = bridge.root / 'codex-runtime' / instance
    folder = home / 'sessions'
    folder.mkdir(mode=0o700, parents=True)
    rollout = folder / ('rollout-fixture-' + native_id + '.jsonl')
    rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {
        'id': native_id, 'cli_version': '0.153.0'}}) + '\n')
    if payload_bytes:
        with rollout.open('a') as out:
            out.write(json.dumps({'type': 'message', 'text': 'x' * payload_bytes}) + '\n')
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT)')
        db.execute('INSERT INTO threads VALUES (?,?)', (native_id, str(rollout)))
    bridge.store.emit(turn, 'assistant', {'text': 'fixture result'})
    return home


def terminal(bridge, turn='turn-1'):
    return next(event for event in bridge.store.events(run_id=turn) if event.kind == 'run_finished')


def scope(bridge):
    value = bridge.checkpoints.identity()
    return {key: value[key] for key in ('owner_ref', 'store_id', 'store_generation')}


def retry(bridge, turn='turn-1', instance='instance'):
    durability = terminal(bridge, turn).data['durability']
    barrier = durability.get('barrier_id') or durability['checkpoint']['barrier_id']
    return bridge.checkpoints.create(format_version='1', operation_id=barrier,
                                     params={**scope(bridge), 'instance_id': instance, 'turn_id': turn})
