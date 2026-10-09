"""SC AB-1: reconnect reads native history and live items before completion."""

import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from agentbridge import Bridge, BridgeError
from agentbridge.process import alive
from agentbridge.rpc import dispatch
from test_interactive_inputs import setup_proxy
from test_message_queues import until


FIXTURE = Path(__file__).parent / 'fixtures/test_native_observation_provider.py'


def test_ab1_native_read_and_stream_survive_client_detach(setup_proxy):
    bridge, instance = setup_proxy(native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    started = bridge.message_create(instance, 'hold', permission_mode='default',
                                    idempotency_key='native-first')
    turn_id = started['turn_id']
    until(lambda: any(event['kind'] == 'item.delta' and event['data'].get('item_id') == 'tool'
                      for event in bridge.turn_events(turn_id)))
    original = bridge.run(turn_id).snapshot
    bridge.close()
    client = Bridge(bridge.root)
    live = dispatch(client, 'instances.read', {'instance_id': instance})
    assert live['status'] == {'type': 'active', 'active_flags': []}
    assert live['native_session_id'] == 'native-thread'
    assert live['owner'] == {'turn_id': turn_id, 'native_turn_id': 'native-turn-1'}
    assert live['turns'][0]['native_turn_id'] == 'native-turn-1'
    items = live['turns'][0]['items']
    assert next(item for item in items if item['item_id'] == 'native-only')['text'] == 'Native history only'
    assert not any(item['type'] == 'reasoning' for item in items)
    assert alive(original['worker_pid'], original['worker_identity'])
    assert len(client.runs()) == 1
    stream = client.turn_events(turn_id)
    assert any(event['kind'] == 'item.started' and event['data']['item'].get('phase') == 'commentary'
               for event in stream)
    assert any(event['kind'] == 'item.delta' and event['data'].get('field') == 'output'
               and event['data'].get('delta') == 'partial output' for event in stream)
    assert not any(event['kind'] == 'turn.completed' for event in stream)
    until(lambda: any(event['data'].get('delta') == ' during read'
                      for event in client.turn_events(turn_id)))
    replayed = {}
    for event in client.instance_events(instance, after_seq=live['after_seq']):
        data = event['data']
        if event['kind'] in {'item.started', 'item.completed'}:
            replayed[data['item_id']] = data['item'].copy()
        elif event['kind'] == 'item.delta':
            target = replayed[data['item_id']]
            target[data['field']] = target.get(data['field'], '') + data['delta']
    merged = {item['item_id']: item for item in items} | replayed
    assert merged['answer']['text'] == 'partial answer during read'
    assert merged['native-only']['text'] == 'Native history only'
    client.message_create(instance, 'finish', delivery='steer')
    until(lambda: client.run(turn_id).status == 'completed')
    until(lambda: not alive(original['worker_pid'], original['worker_identity']))
    completed = dispatch(client, 'instances.reopen', {'instance_id': instance})
    assert completed['status'] == {'type': 'idle'}
    assert 'owner' not in completed
    assert completed['turns'][0]['status'] == 'completed'
    assert len(completed['turns']) == len(client.runs()) == 1
    assert {item['item_id'] for item in completed['turns'][0]['items']} == {
        'user', 'answer', 'tool', 'native-only'}


def test_ab2_interrupt_is_native_and_scoped_and_lookup_never_replays(setup_proxy):
    bridge, instance = setup_proxy(native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    details = bridge.instance_get(instance)
    other = bridge.instance_create(account_ref=details['account_ref'], model=details['model'],
                                    workspace_path=details['workspace_path'])['instance_id']
    first = bridge.message_create(instance, 'hold', delivery='queue', idempotency_key='first')
    second = bridge.message_create(other, 'hold', delivery='queue', idempotency_key='second')
    until(lambda: bridge.message_get(first['message_id']).get('turn_id'))
    until(lambda: bridge.message_get(second['message_id']).get('turn_id'))
    first_id = bridge.message_get(first['message_id'])['turn_id']
    second_id = bridge.message_get(second['message_id'])['turn_id']
    until(lambda: any(e['kind'] == 'item.delta' for e in bridge.turn_events(first_id)))
    until(lambda: any(e['kind'] == 'item.delta' for e in bridge.turn_events(second_id)))
    ack = dispatch(bridge, 'turns.interrupt', {'turn_id': first_id})
    assert ack == {'instance_id': instance, 'turn_id': first_id,
                   'native_session_id': 'native-thread', 'native_turn_id': 'native-turn-1',
                   'acknowledged': True}
    until(lambda: any(e['kind'] == 'turn.completed' and
                      e['data']['turn']['status'] == 'interrupted'
                      for e in bridge.turn_events(first_id)))
    assert bridge.queue_list(instance)['paused']
    assert bridge.instance_read(other)['status']['type'] == 'active'
    receipt = dispatch(bridge, 'messages.lookup', {'instance_id': instance,
                                                  'idempotency_key': 'first'})
    assert receipt['found'] and receipt['message']['message_id'] == first['message_id']
    assert not bridge.message_lookup(instance, 'never-admitted')['found']
    with pytest.raises(BridgeError, match='another instance'):
        bridge.message_lookup(other, 'first')
    assert len(bridge.runs()) == 2
    bridge.turn_interrupt(second_id)
    until(lambda: bridge.run(second_id).status in {'interrupted', 'completed'})


def test_native_idle_read_excludes_concurrent_admission(setup_proxy, monkeypatch):
    from agentbridge import native_reads
    bridge, instance = setup_proxy(native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    started = bridge.message_create(instance, 'hold', permission_mode='default')
    until(lambda: any(e['kind'] == 'item.delta' for e in bridge.turn_events(started['turn_id'])))
    bridge.message_create(instance, 'finish', delivery='steer')
    original = bridge.run(started['turn_id']).snapshot
    until(lambda: not alive(original['worker_pid'], original['worker_identity']))
    entered, release = Event(), Event()
    read = native_reads._disconnected_read
    def held(*args):
        entered.set()
        assert release.wait(5)
        return read(*args)
    monkeypatch.setattr(native_reads, '_disconnected_read', held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(bridge.instance_read, instance)
        assert entered.wait(5)
        try:
            with pytest.raises(BridgeError) as failure:
                bridge.message_create(instance, 'next', permission_mode='default')
            assert failure.value.code == 'busy'
            assert len(bridge.runs()) == 1
        finally:
            release.set()
        assert pending.result(timeout=5)['status']['type'] == 'idle'


def test_native_polling_yields_idle_lock_to_one_queued_followup(setup_proxy, monkeypatch, request):
    from agentbridge import native_reads
    from agentbridge.queueing.connection import request as queue_control

    bridge, instance = setup_proxy(native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    def cleanup():
        try:
            bridge.queue_pause(instance)
        finally:
            try:
                queue_control(bridge.root, instance, {'action': 'shutdown'}, start=False)
                until(lambda: not bridge.queue_list(instance)['dispatcher_running'], timeout=3)
            finally:
                for row in bridge.runs():
                    if any(alive(row[name + '_pid'], row[name + '_identity'])
                           for name in ('worker', 'child')):
                        bridge.turn_recover(row['id'])
    request.addfinalizer(cleanup)
    first = bridge.message_create(instance, 'hold', permission_mode='default',
                                  idempotency_key='read-first')['turn_id']
    assert dispatch(bridge, 'turns.get', {'turn_id': first})['request_key'] == 'read-first'
    until(lambda: any(event['kind'] == 'item.delta' for event in bridge.turn_events(first)))
    bridge.message_create(instance, 'finish', delivery='steer')
    original = bridge.run(first).snapshot
    until(lambda: not alive(original['worker_pid'], original['worker_identity']))
    bridge.queue_pause(instance)
    waiting = bridge.message_create(instance, 'followup', delivery='queue',
                                    idempotency_key='read-followup')
    assert bridge.instance_read(instance)['status']['type'] == 'idle'
    assert bridge.message_lookup(instance, 'read-followup')['message']['message_id'] == (
        waiting['message_id'])
    instance_lock, resumed = native_reads.instance_lock, False

    @contextmanager
    def read_lock(*args, **kwargs):
        nonlocal resumed
        with instance_lock(*args, **kwargs):
            if not resumed:
                # Queue activation is real; this read already owns exclusion, so the
                # dispatcher cannot win the race before the read checks its pending input.
                resumed = True
                bridge.queue_resume(instance)
            yield

    monkeypatch.setattr(native_reads, 'instance_lock', read_lock)
    with pytest.raises(BridgeError) as yielded:
        bridge.instance_read(instance)
    assert yielded.value.code == 'busy'

    def next_owner():
        try:
            observed = bridge.instance_read(instance)
        except BridgeError as error:
            assert error.code in {'busy', 'native_connection_unavailable'}
            return None
        owner = observed.get('owner')
        return observed if owner and owner['turn_id'] != first else None

    observed = until(next_owner)
    next_id = bridge.message_get(waiting['message_id'])['turn_id']
    assert observed['owner']['turn_id'] == next_id
    assert dispatch(bridge, 'turns.get', {'turn_id': next_id})['request_key'] == 'read-followup'
    assert observed['status']['type'] == 'active'
    assert len(bridge.runs()) == 2
    replay = bridge.message_create(instance, 'followup', delivery='queue',
                                   idempotency_key='read-followup')
    assert replay['message_id'] == waiting['message_id'] and len(bridge.runs()) == 2
    bridge.message_create(instance, 'finish', delivery='steer')
    until(lambda: bridge.run(next_id).status == 'completed')

    assert dispatch(bridge, 'turns.get', {'turn_id': next_id})['request_key'] == 'read-followup'
    # A corrupt/stale queue association cannot borrow another instance or turn's key.
    for field, invalid, original in (('session_id', 'other-instance', instance),
                                     ('turn_id', first, next_id)):
        with bridge.store.connect() as db:
            db.execute(f'UPDATE queued_messages SET {field}=? WHERE id=?',
                       (invalid, waiting['message_id']))
        try:
            assert dispatch(bridge, 'turns.get', {'turn_id': next_id})['request_key'] is None
            assert dispatch(bridge, 'turns.get', {'turn_id': first})['request_key'] == 'read-first'
        finally:
            with bridge.store.connect() as db:
                db.execute(f'UPDATE queued_messages SET {field}=? WHERE id=?',
                           (original, waiting['message_id']))


def test_ab2_recovery_stops_only_bound_execution_and_preserves_pending_input(setup_proxy):
    bridge, instance = setup_proxy(native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    details = bridge.instance_get(instance)
    other = bridge.instance_create(account_ref=details['account_ref'], model=details['model'],
                                    workspace_path=details['workspace_path'])['instance_id']
    first = bridge.message_create(instance, 'hold', permission_mode='default')['turn_id']
    second = bridge.message_create(other, 'hold', permission_mode='default')['turn_id']
    for turn_id in (first, second):
        until(lambda: any(e['kind'] == 'item.delta' for e in bridge.turn_events(turn_id)))
    queued = bridge.message_create(instance, 'pending input', delivery='queue',
                                    idempotency_key='pending-input')
    stopped = dispatch(bridge, 'turns.recover', {'turn_id': first})
    assert stopped == {'instance_id': instance, 'turn_id': first,
                       'native_session_id': 'native-thread', 'execution_stopped': True}
    assert bridge.queue_list(instance)['paused']
    assert bridge.message_get(queued['message_id'])['queue_state'] == 'queued'
    assert bridge.instance_read(other)['status']['type'] == 'active'
    assert len(bridge.runs()) == 2
    bridge.queue_delete(instance, queued['message_id'])
    newer = bridge.message_create(instance, 'new explicit action', permission_mode='default')['turn_id']
    until(lambda: any(e['kind'] == 'item.delta' for e in bridge.turn_events(newer)))
    with pytest.raises(BridgeError) as failure:
        bridge.turn_recover(first)
    assert failure.value.code == 'turn_conflict'
    assert bridge.instance_read(instance)['status']['type'] == 'active'
    bridge.turn_recover(newer)
    bridge.turn_recover(second)


def test_ab2_reopen_original_native_id_after_instance_metadata_loss(setup_proxy):
    bridge, instance = setup_proxy(native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    details = bridge.instance_get(instance)
    started = bridge.message_create(instance, 'hold', permission_mode='default')['turn_id']
    until(lambda: any(e['kind'] == 'item.delta' for e in bridge.turn_events(started)))
    bridge.message_create(instance, 'finish', delivery='steer')
    original = bridge.run(started).snapshot
    until(lambda: not alive(original['worker_pid'], original['worker_identity']))
    with bridge.store.connect() as db:
        db.execute('UPDATE sessions SET native_id=NULL WHERE id=?', (instance,))
    assert bridge.instance_reopen(instance, native_session_id='native-thread')['native_session_id'] == 'native-thread'
    with bridge.store.connect() as db:
        for table, column in (('sessions', 'id'), ('instance_metadata', 'session_id'),
                              ('session_routing', 'session_id'),
                              ('instance_execution_policies', 'session_id')):
            db.execute(f'DELETE FROM {table} WHERE {column}=?', (instance,))
    with pytest.raises(BridgeError) as missing:
        bridge.instance_reopen(instance)
    assert missing.value.code == 'native_binding_required'
    restored = dispatch(bridge, 'instances.reopen', {
        'instance_id': instance, 'native_session_id': 'native-thread',
        'account_ref': details['account_ref'], 'workspace_path': details['workspace_path'],
        'model': details['model']})
    assert restored['native_session_id'] == 'native-thread'
    assert len(restored['turns']) == len(bridge.runs()) == 1
    with pytest.raises(BridgeError) as mismatch:
        bridge.instance_reopen(instance, native_session_id='another-thread')
    assert mismatch.value.code == 'native_session_diverged'
    history = bridge.root / 'codex-runtime' / instance / 'fixture-native-thread.json'
    history.unlink()
    with pytest.raises(BridgeError) as absent:
        bridge.instance_reopen(instance)
    assert absent.value.code == 'native_thread_missing'
    assert len(bridge.runs()) == 1
    home = history.parent
    home.rename(home.with_name(home.name + '-unavailable'))
    with pytest.raises(BridgeError) as unavailable:
        bridge.instance_read(instance)
    assert unavailable.value.code == 'native_history_unavailable'
    assert not home.exists()


def test_native_owner_retains_safe_rejection_details_without_replaying_or_stopping(
        setup_proxy, request, monkeypatch):
    from agentbridge.error_evidence import capture
    from agentbridge.native_control import address

    bridge, instance = setup_proxy(native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    started = bridge.message_create(instance, 'reject-controls', permission_mode='default',
                                    idempotency_key='one-native-request')['turn_id']

    def cleanup():
        row = bridge.run(started).snapshot
        if any(alive(row[name + '_pid'], row[name + '_identity']) for name in ('worker', 'child')):
            bridge.turn_recover(started)

    request.addfinalizer(cleanup)
    until(lambda: any(event['kind'] == 'item.delta' for event in bridge.turn_events(started)))
    owner = bridge.run(started).snapshot
    for action, method, native_code in (('read', 'thread_read', -32600),
                                        ('interrupt', 'turn_interrupt', -32602)):
        with pytest.raises(BridgeError) as rejected:
            if action == 'read':
                dispatch(bridge, 'instances.read', {'instance_id': instance})
            else:
                dispatch(bridge, 'turns.interrupt', {'turn_id': started})
        error = rejected.value.safe_data()
        assert error['code'] == 'provider_failed'
        assert error['outcome'] == ('unknown' if action == 'interrupt' else 'not_started')
        assert error['retryable'] is False
        assert error['details'] == {
            'detection': 'unclassified', 'http_status': 418,
            'native_method': method, 'native_code': native_code,
            'unknown_evidence': capture({'code': native_code,
                'message': 'private-native-canary capacity rejected',
                'data': {'httpStatusCode': 418, 'private': 'private-native-canary'}})}
        assert 'private-native-canary' not in json.dumps(error)
        live = bridge.instance_read(instance)
        assert live['owner']['turn_id'] == started
        assert live['status']['type'] == 'active'
        current = bridge.run(started).snapshot
        assert current['child_pid'] == owner['child_pid']
        assert alive(current['child_pid'], current['child_identity'])
        assert len(bridge.runs()) == 1
        assert bridge.message_lookup(instance, 'one-native-request')['message']['turn_id'] == started
    # Exercise an older owner's code-only reply and reject malformed diagnostic fields
    # at the same socket boundary. Only this fixture's successful read reply is replaced.
    for diagnostic in (None, {'detection': ['private-native-canary'], 'http_status': True,
            'provider_code': 'private-native-canary', 'native_method': {'private': 'canary'},
            'native_code': True, 'unknown_evidence': {'fingerprint': 'private-native-canary'}}):
        decode = json.loads
        def owner_reply(payload, *args, **kwargs):
            value = decode(payload, *args, **kwargs)
            if (isinstance(value, dict) and value.get('ok') is True
                    and isinstance(value.get('result'), dict)
                    and value['result'].get('native_session_id') == 'native-thread'):
                value = {'ok': False, 'code': 'provider_failed'}
                if diagnostic is not None:
                    value['details'] = diagnostic
            return value
        with monkeypatch.context() as legacy:
            legacy.setattr(json, 'loads', owner_reply)
            with pytest.raises(BridgeError) as rejected:
                bridge.instance_read(instance)
            assert rejected.value.code == 'provider_failed'
            assert rejected.value.details == {}
            assert 'private-native-canary' not in json.dumps(rejected.value.safe_data())
    assert bridge.instance_read(instance)['owner']['turn_id'] == started
    assert bridge.turn_interrupt(started)['acknowledged'] is True
    until(lambda: bridge.run(started).status == 'interrupted')
    until(lambda: not alive(owner['worker_pid'], owner['worker_identity']))
    with address(bridge.root, instance, started) as (endpoint, _):
        assert not endpoint.exists()
    assert len(bridge.runs()) == 1
