"""Durability is persisted before admission reopens; failures preserve historical results."""

import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.checkpoint import content, native, persistence
from agentbridge.errors import BridgeError
from agentbridge.queueing.persistence import QueueStore
from fixtures.test_checkpoint_fixture import execution, prepared, retry, scope, terminal


def test_seal_and_sqlite_snapshot_are_coherent_and_idempotent(tmp_path):
    bridge = prepared(tmp_path)
    home = execution(bridge, payload_bytes=2 * 1024 * 1024)
    (home / 'auth.json').write_text('synthetic-secret-excluded')
    started = time.monotonic()
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    elapsed = time.monotonic() - started
    result = terminal(bridge)
    checkpoint = result.data['durability']['checkpoint']
    events = bridge.store.events(run_id='turn-1')
    ready = next(event for event in events if event.kind == 'checkpoint_ready')
    assert checkpoint['execution_cursor']['seq'] < result.seq < ready.seq
    assert checkpoint['sealed_at'] >= ready.data['checkpoint']['sealed_at']
    assert ready.data['final'] is False
    assert checkpoint['native_id'] == bridge.get_session('instance')['native_id']
    assert all(item['path'] != 'auth.json' for item in checkpoint['files'])
    assert checkpoint['content']['bytes'] > 2 * 1024 * 1024
    assert retry(bridge) == checkpoint
    assert len(bridge.store.events(run_id='turn-1')) == len(events)
    identity = bridge.checkpoints.identity()
    cursor = {**identity['cursor'], 'seq': 0}
    page = bridge.turn_events('turn-1', cursor=cursor)
    assert page[-1]['kind'] == 'checkpoint.ready' and page[-1]['final'] is False
    assert page[-1]['cursor']['seq'] == ready.seq
    op = str(uuid4())
    snapshot = bridge.checkpoints.snapshot_store(format_version='1', operation_id=op,
                                                params=scope(bridge))
    assert snapshot['coverage'] == [{
        'instance_id': 'instance', 'native_id': checkpoint['native_id'], 'turn_id': 'turn-1',
        'checkpoint_id': checkpoint['checkpoint_id'], 'execution_seq': checkpoint['execution_cursor']['seq'],
        'terminal_seq': result.seq, 'checkpoint_event_seq': ready.seq}]
    assert snapshot['cursor']['seq'] >= ready.seq
    assert bridge.checkpoints.snapshot_store(format_version='1', operation_id=op,
                                             params=scope(bridge)) == snapshot
    print(json.dumps({'native_bytes': sum(item['bytes'] for item in checkpoint['files']),
                      'archive_bytes': checkpoint['content']['bytes'], 'seal_seconds': elapsed}))


def test_failed_seal_blocks_mutations_but_not_other_instances_and_retry_is_stable(tmp_path, monkeypatch):
    bridge = prepared(tmp_path)
    execution(bridge)
    original = content.publish_descriptor
    monkeypatch.setattr(content, 'publish_descriptor', lambda *_: (_ for _ in ()).throw(
        OSError('synthetic-secret-must-not-be-persisted')))
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    assert bridge.run('turn-1').status == 'completed'
    assert terminal(bridge).data['durability']['state'] == 'pending'
    calls = [lambda: bridge.store.admit('turn-2', 'instance', 'next', RunOptions(), 'next'),
             lambda: bridge.store.update_session('instance', model='other'),
             lambda: bridge.instance_delete('instance')]
    for action in calls:
        with pytest.raises(BridgeError) as caught:
            action()
        assert caught.value.code == 'checkpoint_pending'
    bridge.store.add_session('independent', 'fixture', str(tmp_path), 'fixture-model')
    assert bridge.store.admit('other', 'independent', 'hello', RunOptions(), 'other')[1]
    bridge.store.finish('other', 'failed')
    assert terminal(bridge, 'other').data['durability']['reason'] == 'not_started'
    if bridge.store.path is not None:
        assert b'synthetic-secret-must-not-be-persisted' not in bridge.store.path.read_bytes()
    monkeypatch.setattr(content, 'publish_descriptor', original)
    restarted = Bridge(bridge.root)
    descriptor = retry(restarted)
    events = restarted.store.events(run_id='turn-1')
    assert [event.kind for event in events].count('run_finished') == 1
    assert [event.kind for event in events].count('checkpoint_ready') == 1
    assert events[-1].data['checkpoint'] == descriptor
    assert terminal(restarted).data['durability']['state'] == 'pending'
    assert retry(restarted) == descriptor
    assert restarted.store.admit('next', 'instance', 'next', RunOptions(), 'next')[1]


def test_pending_descendant_proof_cannot_be_bypassed_by_restart_or_request(tmp_path):
    bridge = prepared(tmp_path)
    execution(bridge)
    bridge.store.finish('turn-1', 'completed', process_verified=False)
    restored = Bridge(bridge.root)
    with pytest.raises(BridgeError) as caught:
        retry(restored)
    assert caught.value.code == 'checkpoint_busy'
    assert terminal(restored).data['durability']['state'] == 'pending'
    with pytest.raises(BridgeError):
        restored.checkpoints.create(format_version='1', operation_id=str(uuid4()), params={
            **scope(restored), 'instance_id': 'instance', 'turn_id': 'turn-1',
            'process_verified': True})
    historical = terminal(restored).data
    barrier = historical['durability']['barrier_id']
    proof_ref = str(uuid4())
    with pytest.raises(BridgeError):
        restored.checkpoints.adopt_drained('instance', operation_id=str(uuid4()),
                                          proof_ref=proof_ref, verify_quiescence=lambda _: True)
    with pytest.raises(BridgeError):
        restored.checkpoints.adopt_drained('instance', operation_id=barrier,
                                          proof_ref=proof_ref, verify_quiescence=lambda _: False)
    with pytest.raises(BridgeError):
        retry(restored)
    observed = []

    def trusted_closed_boundary(binding):
        observed.append(binding)
        return True

    descriptor = restored.checkpoints.adopt_drained('instance', operation_id=barrier,
        proof_ref=proof_ref, verify_quiescence=trusted_closed_boundary)
    assert observed == [{**scope(restored), 'instance_id': 'instance', 'turn_id': 'turn-1',
                         'barrier_id': barrier}]
    assert descriptor['barrier_id'] == barrier
    assert terminal(restored).data == historical
    assert restored.store.events(run_id='turn-1')[-1].kind == 'checkpoint_ready'
    assert restored.store.admit('next', 'instance', 'next', RunOptions(), 'next')[1]


def test_native_symlink_or_drift_is_pending_and_corrupt_archive_is_rejected(tmp_path):
    bridge = prepared(tmp_path)
    home = execution(bridge)
    (home / 'unsafe').symlink_to(tmp_path / 'outside')
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    assert terminal(bridge).data['durability']['error']['code'] == 'checkpoint_corrupt'
    (home / 'unsafe').unlink()
    descriptor = retry(bridge)
    archive = bridge.checkpoints.resolve_content(descriptor['content'])
    archive.chmod(0o600)
    archive.write_bytes(b'corrupt')
    with pytest.raises(BridgeError) as caught:
        retry(bridge)
    assert caught.value.code == 'checkpoint_corrupt'


def test_cursor_binding_and_legacy_mode_are_explicit(tmp_path):
    bridge = prepared(tmp_path)
    identity = bridge.checkpoints.identity()
    assert bridge.capabilities()['durability']['enabled'] is True
    for supplied, code in ((None, 'cursor_generation_mismatch'),
                           ({**identity['cursor'], 'store_generation': str(uuid4())},
                            'cursor_generation_mismatch'),
                           ({**identity['cursor'], 'seq': 999999}, 'cursor_rollback')):
        with pytest.raises(BridgeError) as caught:
            bridge.instance_events('instance', cursor=supplied)
        assert caught.value.code == code
    with pytest.raises(BridgeError):
        Bridge(bridge.root, owner_ref='another-owner')
    with pytest.raises(BridgeError):
        Bridge(bridge.root, durable=False)
    legacy = prepared(tmp_path / 'legacy', durable=False)
    legacy.store.admit('unstarted', 'instance', 'hello', RunOptions(), 'once')
    legacy.store.finish('unstarted', 'failed')
    assert 'durability' not in terminal(legacy, 'unstarted').data
    assert legacy.capabilities()['durability']['support'] == 'disabled'


def test_worker_loss_after_publication_finishes_saved_intent_once(tmp_path, monkeypatch):
    bridge = prepared(tmp_path)
    execution(bridge)
    original = content.publish_descriptor

    def crash(store, descriptor):
        original(store, descriptor)
        raise SystemExit('fixture abrupt worker exit')

    monkeypatch.setattr(content, 'publish_descriptor', crash)
    with pytest.raises(SystemExit):
        bridge.store.finish('turn-1', 'completed', process_verified=True)
    with bridge.store.connect() as db:
        db.execute("UPDATE runs SET created=0 WHERE id='turn-1'")
    assert bridge.run('turn-1').status == 'starting'
    monkeypatch.setattr(content, 'publish_descriptor', original)
    restarted = Bridge(bridge.root)
    restarted.recover()
    assert restarted.run('turn-1').status == 'completed'
    assert terminal(restarted).data['durability']['state'] == 'sealed'
    assert len([e for e in restarted.store.events(run_id='turn-1') if e.kind == 'run_finished']) == 1


def test_queue_waits_for_pending_seal_without_mutating_order_or_versions(tmp_path, monkeypatch):
    from agentbridge.queueing.worker import Dispatcher

    bridge = prepared(tmp_path)
    execution(bridge)
    queue = QueueStore(bridge.store)
    message, _ = queue.add('instance', 'queued next', RunOptions(), (), 'queued', 'digest')
    queue.activate('instance', message)
    monkeypatch.setattr(native, 'materialize', lambda *_: native.fail('checkpoint_busy'))
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    before = queue.snapshot('instance')
    dispatcher = Dispatcher(bridge.root, 'instance')
    called = []
    monkeypatch.setattr(dispatcher.bridge, 'submit', lambda *args, **kwargs: called.append(args))
    assert dispatcher.advance() is True and called == []
    after = queue.snapshot('instance')
    assert (after['items'], after['version']) == (before['items'], before['version'])
    with pytest.raises(BridgeError) as caught:
        queue.add('instance', 'another', RunOptions(), (), 'another', 'different')
    assert caught.value.code == 'checkpoint_pending'
    monkeypatch.undo()
    retry(bridge)
    monkeypatch.setattr(dispatcher.bridge, 'submit', lambda *args, **kwargs: called.append(args))
    assert dispatcher.advance() is True and len(called) == 1


def test_sqlite_snapshot_during_other_instance_writes_has_only_its_committed_frontier(tmp_path):
    from threading import Event, Thread

    bridge = prepared(tmp_path)
    execution(bridge)
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    bridge.store.add_session('other', 'fixture', str(tmp_path), 'fixture-model')
    bridge.store.admit('active', 'other', 'hello', RunOptions(), 'active')
    started, stop = Event(), Event()

    def writer():
        started.set()
        for number in range(100):
            if stop.is_set():
                return
            bridge.store.emit('active', 'text_delta', {'text': str(number)})
            time.sleep(.001)

    thread = Thread(target=writer)
    thread.start()
    started.wait(2)
    try:
        snapshot = bridge.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                    params=scope(bridge))
    finally:
        stop.set()
        thread.join(timeout=5)
    with sqlite3.connect(bridge.checkpoints.resolve_content(snapshot['content'])) as db:
        maximum = db.execute('SELECT MAX(seq) FROM events').fetchone()[0]
        assert maximum == snapshot['cursor']['seq']
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        for covered in snapshot['coverage']:
            assert db.execute('SELECT kind FROM events WHERE seq=?',
                              (covered['checkpoint_event_seq'],)).fetchone()[0] == 'checkpoint_ready'
    assert [item['instance_id'] for item in snapshot['coverage']] == ['instance']


def test_persisted_terminal_intent_survives_worker_error_retry(tmp_path, monkeypatch):
    bridge = prepared(tmp_path)
    execution(bridge)
    original = content.publish_descriptor

    def crash(store, descriptor):
        original(store, descriptor)
        raise SystemExit('fixture crash before terminal transaction')

    monkeypatch.setattr(content, 'publish_descriptor', crash)
    with pytest.raises(SystemExit):
        bridge.store.finish('turn-1', 'completed', process_verified=True)
    monkeypatch.setattr(content, 'publish_descriptor', original)
    bridge.store.finish('turn-1', 'interrupted', 'worker_failed', process_verified=True)
    assert terminal(bridge).data['state'] == 'completed'
    assert terminal(bridge).data['code'] is None
    assert terminal(bridge).data['durability']['state'] == 'sealed'
