"""Offline recovery restores verified native histories held, without replay or authority reuse."""

from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from uuid import uuid4

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.checkpoint.snapshot import restore_store
from agentbridge.checkpoint import native
from agentbridge.errors import BridgeError
from fixtures.test_checkpoint_fixture import execution, prepared, scope, terminal


def sealed(tmp_path, *, wal_indexes=False):
    bridge = prepared(tmp_path)
    home = execution(bridge)
    if wal_indexes:
        for name in ('logs_2.sqlite', 'queue_1.sqlite'):
            path = home / name
            with closing(sqlite3.connect(path)) as db, db:
                assert db.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
                db.execute('CREATE TABLE entries(value TEXT)')
                db.execute('INSERT INTO entries VALUES (?)', (name,))
            # A running source has already opened its indexes; restored copies have not.
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('PRAGMA schema_version').fetchone()
            assert path.with_name(name + '-wal').stat().st_size == 0
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    checkpoint = terminal(bridge).data['durability']['checkpoint']
    snapshot = bridge.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                params=scope(bridge))
    return bridge, checkpoint, snapshot


def imported(tmp_path, *, wal_indexes=False):
    bridge, checkpoint, snapshot = sealed(tmp_path, wal_indexes=wal_indexes)
    binding = restore_store(tmp_path / 'restored', snapshot,
                            bridge.checkpoints.resolve_content(snapshot['content']),
                            owner_ref=snapshot['owner_ref'])
    assert not list((tmp_path / 'restored').glob('bridge.sqlite3-*'))
    with sqlite3.connect('file:' + str(tmp_path / 'restored/bridge.sqlite3') + '?immutable=1',
                         uri=True) as imported_db:
        assert imported_db.execute('SELECT store_generation FROM store_identity').fetchone()[0] == (
            binding['store_generation'])
    restored = Bridge(tmp_path / 'restored')
    restored.checkpoints.register_content(checkpoint['content'],
                                          bridge.checkpoints.resolve_content(checkpoint['content']))
    request = {'format_version': '1', 'operation_id': str(uuid4()), 'params': {
        'checkpoint': checkpoint, 'content': checkpoint['content'],
        'destination_generation': binding['store_generation']}}
    return bridge, restored, checkpoint, snapshot, request


def test_restored_store_and_native_history_stay_held_until_explicit_host_release(tmp_path):
    bridge, restored, checkpoint, snapshot, request = imported(tmp_path)
    assert restored.checkpoints.identity()['store_id'] == snapshot['store_id']
    assert restored.checkpoints.identity()['store_generation'] != snapshot['store_generation']
    with pytest.raises(BridgeError) as caught:
        restored.instance_events('instance', cursor=snapshot['cursor'])
    assert caught.value.code == 'cursor_generation_mismatch'
    identity = restored.checkpoints.identity()
    assert identity['source_cursor'] == snapshot['cursor']
    with pytest.raises(BridgeError) as caught:
        restored.instance_events('instance', cursor={**identity['cursor'], 'seq': 0})
    assert caught.value.code == 'cursor_generation_mismatch'
    assert restored.instance_events('instance', cursor=identity['replay_start']) == []
    with pytest.raises(BridgeError):
        restored.store.admit('next', 'instance', 'next', RunOptions(), 'next')
    with pytest.raises(BridgeError):
        restored.checkpoints.release_recovery(expected_generation=request['params']['destination_generation'])
    result = restored.checkpoints.restore(**request)
    assert result['state'] == 'restored_held'
    assert result['native_id'] == checkpoint['native_id']
    assert restored.checkpoints.restore(**request) == result
    home = restored.root / 'codex-runtime' / 'instance'
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        native_id, rollout = db.execute('SELECT id,rollout_path FROM threads').fetchone()
    assert native_id == checkpoint['native_id']
    assert rollout.startswith(str(home)) and not rollout.startswith(str(bridge.root))
    restored.checkpoints.release_recovery(expected_generation=result['store_generation'])
    assert not restored.checkpoints.identity()['recovery_held']
    replay = restored.store.admit('ignored', 'instance', 'hello', RunOptions(model='fixture-model'), 'turn-1')
    assert replay == ('turn-1', False)
    assert restored.store.admit('next', 'instance', 'next', RunOptions(), 'next')[1]
    assert len(restored.store.events(run_id='turn-1')) == len(bridge.store.events(run_id='turn-1'))
    assert '.agentbridge-checkpoint.json' not in {path for path, _ in native.inventory(home)}


def test_scope_runtime_and_diverged_retry_fail_before_release(tmp_path, monkeypatch):
    _, restored, checkpoint, _, request = imported(tmp_path)
    bad = deepcopy(request)
    bad['params']['destination_generation'] = str(uuid4())
    with pytest.raises(BridgeError) as caught:
        restored.checkpoints.restore(**bad)
    assert caught.value.code == 'scope_mismatch'
    assert not (restored.root / 'codex-runtime' / 'instance').exists()
    original = native.runtime
    monkeypatch.setattr(native, 'runtime', lambda *args, **kwargs:
                        {**checkpoint['runtime'], 'native_digest': 'f' * 64})
    with pytest.raises(BridgeError) as caught:
        restored.checkpoints.restore(**request)
    assert caught.value.code == 'runtime_incompatible'
    assert not (restored.root / 'codex-runtime' / 'instance').exists()
    monkeypatch.setattr(native, 'runtime', original)
    restored.checkpoints.restore(**request)
    home = restored.root / 'codex-runtime' / 'instance'
    next(home.rglob('*.jsonl')).write_text('diverged history')
    with pytest.raises(BridgeError) as caught:
        restored.checkpoints.restore(**request)
    assert caught.value.code == 'checkpoint_corrupt'
    with pytest.raises(BridgeError):
        restored.checkpoints.release_recovery(expected_generation=request['params']['destination_generation'])


def test_active_snapshot_never_replays_uncertain_work_or_restores_process_authority(tmp_path):
    bridge = prepared(tmp_path)
    execution(bridge)
    with bridge.store.connect() as db:
        db.execute("UPDATE runs SET worker_pid=999999,child_pid=999998,"
                   "worker_identity='old-owner',child_identity='old-child' WHERE id='turn-1'")
    snapshot = bridge.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                params=scope(bridge))
    assert snapshot['coverage'] == []
    binding = restore_store(tmp_path / 'restored', snapshot,
                            bridge.checkpoints.resolve_content(snapshot['content']),
                            owner_ref=snapshot['owner_ref'])
    restored = Bridge(tmp_path / 'restored')
    row = restored.store.get('runs', 'turn-1')
    assert row['state'] == 'interrupted' and row['error'] == 'restore_unknown_outcome'
    assert row['worker_pid'] is None and row['child_pid'] is None
    assert row['worker_identity'] is None and row['child_identity'] is None
    assert terminal(restored).data['outcome'] == 'unknown'
    assert terminal(restored).data['durability']['state'] == 'pending'
    assert restored.store.events(run_id='turn-1')[-1].kind == 'checkpoint_pending'
    with pytest.raises(BridgeError) as caught:
        restored.checkpoints.release_recovery(expected_generation=binding['store_generation'])
    assert caught.value.code == 'continuity_pending'


def test_legacy_adoption_requires_host_proof_and_does_not_rewrite_old_terminal(tmp_path):
    legacy = prepared(tmp_path, durable=False)
    execution(legacy)
    legacy.store.finish('turn-1', 'completed')
    historical = terminal(legacy).data
    assert 'durability' not in historical
    bridge = Bridge(legacy.root, durable=True)
    operation_id, proof_ref = str(uuid4()), str(uuid4())
    observed = []

    def refused(scope):
        observed.append(scope)
        return False

    with pytest.raises(BridgeError) as caught:
        bridge.checkpoints.adopt_drained('instance', operation_id=operation_id,
                                         proof_ref=proof_ref, verify_quiescence=refused)
    assert caught.value.code == 'checkpoint_busy'
    assert observed[0]['store_generation'] == scope(bridge)['store_generation']
    with pytest.raises(BridgeError):
        bridge.store.admit('next', 'instance', 'next', RunOptions(), 'next')
    descriptor = bridge.checkpoints.adopt_drained('instance', operation_id=operation_id,
                                                 proof_ref=proof_ref, verify_quiescence=lambda _: True)
    assert descriptor['turn_id'] == 'turn-1'
    assert terminal(bridge).data == historical
    events = bridge.store.events(run_id='turn-1')
    assert [event.kind for event in events][-2:] == ['checkpoint_pending', 'checkpoint_ready']


def test_restored_generation_needs_new_seal_and_supports_a_second_isolated_restore(tmp_path):
    _, restored, original, _, request = imported(tmp_path, wal_indexes=True)
    result = restored.checkpoints.restore(**request)
    home = restored.root / 'codex-runtime' / 'instance'
    assert not list(home.glob('logs_2.sqlite-*'))
    assert not list(home.glob('queue_1.sqlite-*'))
    restored.checkpoints.release_recovery(expected_generation=result['store_generation'])
    without_new_seal = restored.checkpoints.snapshot_store(
        format_version='1', operation_id=str(uuid4()), params=scope(restored))
    assert without_new_seal['coverage'] == []
    historical = terminal(restored).data
    checkpoint = restored.checkpoints.adopt_drained(
        'instance', operation_id=str(uuid4()), proof_ref=str(uuid4()), verify_quiescence=lambda _: True)
    assert checkpoint['checkpoint_id'] != original['checkpoint_id']
    assert checkpoint['store_generation'] == result['store_generation']
    assert terminal(restored).data == historical
    for name in ('logs_2.sqlite', 'queue_1.sqlite'):
        assert (home / (name + '-wal')).stat().st_size == 0
        with closing(sqlite3.connect((home / name).as_uri() + '?mode=ro', uri=True)) as db:
            assert db.execute('SELECT value FROM entries').fetchall() == [(name,)]
    snapshot = restored.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                 params=scope(restored))
    assert snapshot['coverage'][0]['checkpoint_id'] == checkpoint['checkpoint_id']
    with restored.store.connect() as db:
        db.execute('INSERT OR REPLACE INTO recovery_workspaces VALUES (?,?,?)',
                   ('instance', '/obsolete-source', '/obsolete-target'))
    snapshot = restored.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                 params=scope(restored))
    binding = restore_store(tmp_path / 'second', snapshot,
                            restored.checkpoints.resolve_content(snapshot['content']),
                            owner_ref=snapshot['owner_ref'])
    second = Bridge(tmp_path / 'second')
    with second.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM recovery_workspaces').fetchone()[0] == 0
    with pytest.raises(BridgeError):
        second.checkpoints.release_recovery(expected_generation=binding['store_generation'])
    second.checkpoints.register_content(checkpoint['content'],
                                        restored.checkpoints.resolve_content(checkpoint['content']))
    second.checkpoints.restore(format_version='1', operation_id=str(uuid4()), params={
        'checkpoint': checkpoint, 'content': checkpoint['content'],
        'destination_generation': binding['store_generation']})
    second.checkpoints.release_recovery(expected_generation=binding['store_generation'])
    assert second.get_session('instance')['native_id'] == original['native_id']
    # Stability must still notice committed WAL data without a change to the main file.
    second_home = second.root / 'codex-runtime' / 'instance'
    path = second_home / 'logs_2.sqlite'
    before, main_before = native.fingerprint(second_home), native.digest(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO entries VALUES ('changed')")
        db.commit()
        assert path.with_name(path.name + '-wal').stat().st_size > 0
        assert native.digest(path) == main_before
        assert native.fingerprint(second_home) != before


def test_partial_release_keeps_unknown_held_and_later_restores_another_instance(tmp_path):
    from agentbridge.queueing.persistence import QueueStore
    from agentbridge.rpc import dispatch

    bridge, checkpoint, _ = sealed(tmp_path)
    for instance in ('later', 'unknown'):
        bridge.store.add_session(instance, 'fixture', str(tmp_path), 'fixture-model')
        execution(bridge, instance=instance, turn=instance)
    bridge.store.finish('later', 'completed', process_verified=True)
    later_checkpoint = terminal(bridge, 'later').data['durability']['checkpoint']
    snapshot = bridge.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                params=scope(bridge))
    binding = restore_store(tmp_path / 'partial', snapshot,
                            bridge.checkpoints.resolve_content(snapshot['content']),
                            owner_ref=snapshot['owner_ref'])
    restored = Bridge(tmp_path / 'partial')
    generation = binding['store_generation']

    def materialize(descriptor):
        restored.checkpoints.register_content(descriptor['content'],
                                              bridge.checkpoints.resolve_content(descriptor['content']))
        restored.checkpoints.restore(format_version='1', operation_id=str(uuid4()), params={
            'checkpoint': descriptor, 'content': descriptor['content'],
            'destination_generation': generation})

    materialize(checkpoint)
    historical = terminal(restored, 'unknown').data
    with pytest.raises(BridgeError):
        restored.checkpoints.release_recovery(expected_generation=generation)
    restored.checkpoints.release_recovery(expected_generation=generation,
                                          ready_instances=['instance'])
    restored = Bridge(restored.root)
    identity = restored.checkpoints.identity()
    assert not identity['recovery_held']
    assert [row['instance_id'] for row in identity['continuity_holds']] == ['later', 'unknown']
    for action in (
            lambda: restored.store.admit('next', 'unknown', 'next', RunOptions(), 'next'),
            lambda: restored.store.update_session('unknown', model='other'),
            lambda: restored.instance_delete('unknown'),
            lambda: QueueStore(restored.store).add('unknown', 'next', RunOptions(), (), 'key', 'hash'),
            lambda: restored.checkpoints.adopt_drained('unknown', operation_id=str(uuid4()),
                proof_ref=str(uuid4()), verify_quiescence=lambda _: True)):
        with pytest.raises(BridgeError):
            action()
    for method in ('instances.archive', 'instances.delete', 'instances.discard_evaluation',
                   'queues.pause'):
        with pytest.raises(BridgeError) as caught:
            dispatch(restored, method, {'instance_id': 'unknown'})
        assert caught.value.code == 'checkpoint_pending'
    with pytest.raises(BridgeError) as caught:
        dispatch(restored, 'checkpoints.release_recovery',
                 {'expected_generation': generation, 'ready_instances': ['unknown']})
    assert caught.value.code == 'method_not_found'
    with pytest.raises(BridgeError) as caught:
        restored.checkpoints.release_recovery(expected_generation=generation,
                                              ready_instances=['later'])
    assert caught.value.code == 'checkpoint_incomplete'
    materialize(later_checkpoint)
    restored.checkpoints.release_recovery(expected_generation=generation,
                                          ready_instances=['later'])
    with pytest.raises(BridgeError) as caught:
        restored.checkpoints.release_recovery(expected_generation=generation,
                                              ready_instances=['unknown'])
    assert caught.value.code == 'continuity_pending'
    assert terminal(restored, 'unknown').data == historical
    restored.checkpoints.adopt_drained('instance', operation_id=str(uuid4()),
                                       proof_ref=str(uuid4()), verify_quiescence=lambda _: True)
    captured = restored.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                   params=scope(restored))
    assert [row['instance_id'] for row in captured['coverage']] == ['instance']
    with sqlite3.connect(restored.checkpoints.resolve_content(captured['content'])) as db:
        assert db.execute('SELECT instance_id FROM continuity_holds').fetchall() == [('unknown',)]
    assert restored.store.admit('next', 'instance', 'next', RunOptions(), 'next')[1]
    assert restored.store.admit('later-next', 'later', 'next', RunOptions(), 'later-next')[1]


def test_restore_fsync_failure_preserves_hold_and_same_operation_retries(tmp_path, monkeypatch):
    from agentbridge.checkpoint import content

    _, restored, _, _, request = imported(tmp_path)
    original = content.sync_tree
    observed = []
    sync_directory = content.sync_directory

    def record(path):
        observed.append(str(path))
        sync_directory(path)

    def fail_before_publication(path):
        original(path)
        raise OSError('synthetic-sensitive-io-body')

    monkeypatch.setattr(content, 'sync_tree', fail_before_publication)
    with pytest.raises(BridgeError) as caught:
        restored.checkpoints.restore(**request)
    assert caught.value.code == 'checkpoint_incomplete' and caught.value.retryable
    assert 'synthetic-sensitive' not in str(caught.value)
    assert restored.checkpoints.identity()['recovery_held']
    assert not (restored.root / 'codex-runtime/instance').exists()
    with restored.store.connect() as db:
        assert db.execute('SELECT state FROM checkpoint_restore_operations').fetchone()[0] == 'publishing'
    monkeypatch.setattr(content, 'sync_tree', original)
    monkeypatch.setattr(content, 'sync_directory', record)
    result = restored.checkpoints.restore(**request)
    stage = next(path for path in reversed(observed) if '/.restore-native-' in path)
    assert observed.index(stage + '/sessions') < observed.index(stage)
    assert observed.index(stage) < len(observed) - 1
    assert observed[-1] == str(restored.root / 'codex-runtime')
    assert restored.checkpoints.restore(**request) == result
    restored.checkpoints.release_recovery(expected_generation=result['store_generation'])


def test_new_private_directory_entries_sync_all_new_parents(tmp_path, monkeypatch):
    from agentbridge.checkpoint import content

    observed = []
    original = content.sync_directory

    def record(path):
        observed.append(path)
        original(path)

    monkeypatch.setattr(content, 'sync_directory', record)
    folder = tmp_path / 'new-store/checkpoints/objects'
    native.private_directory(folder)
    assert all(parent in observed for parent in (folder, folder.parent, folder.parent.parent, tmp_path))
    observed.clear()
    native.private_directory(folder)
    assert observed == []


@pytest.mark.parametrize('outside_auxiliary', [False, True])
def test_bd2_restore_uses_authenticated_native_origin_after_session_workspace_changes(
        tmp_path, outside_auxiliary):
    bridge = prepared(tmp_path)
    home = execution(bridge)
    origin = '/historical/native-workspace'
    latest = tmp_path / 'latest-workspace'
    latest.mkdir(mode=0o700)
    with bridge.store.connect() as db:
        db.execute('UPDATE sessions SET cwd=? WHERE id=?', (str(latest), 'instance'))
    with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('ALTER TABLE threads ADD COLUMN cwd TEXT')
        db.execute('UPDATE threads SET cwd=?', (origin,))
        if outside_auxiliary:
            auxiliary = str(uuid4())
            rollout = home / 'sessions' / ('rollout-auxiliary-' + auxiliary + '.jsonl')
            rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {
                'id': auxiliary, 'cli_version': '0.153.0'}}) + '\n')
            db.execute('INSERT INTO threads VALUES (?,?,?)',
                       (auxiliary, str(rollout), '/unrelated-workspace'))
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    checkpoint = terminal(bridge).data['durability']['checkpoint']
    original = deepcopy(checkpoint)
    snapshot = bridge.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                params=scope(bridge))
    destination = tmp_path / 'protected-workspace'
    destination.mkdir(mode=0o500)
    binding = restore_store(tmp_path / 'restored', snapshot,
                            bridge.checkpoints.resolve_content(snapshot['content']),
                            owner_ref=snapshot['owner_ref'],
                            workspace_paths={'instance': destination})
    restored = Bridge(tmp_path / 'restored')
    restored.checkpoints.register_content(checkpoint['content'],
                                          bridge.checkpoints.resolve_content(checkpoint['content']))
    request = {'format_version': '1', 'operation_id': str(uuid4()), 'params': {
        'checkpoint': checkpoint, 'content': checkpoint['content'],
        'destination_generation': binding['store_generation']}}
    with pytest.raises(BridgeError) as caught:
        restored.checkpoints.restore(**request)
    assert caught.value.code == 'scope_mismatch'
    if outside_auxiliary:
        with pytest.raises(BridgeError) as caught:
            restored.checkpoints.prepare_restore_workspace(**request)
        assert caught.value.code == 'scope_mismatch'
        with restored.store.connect() as db:
            row = db.execute('SELECT source_path FROM recovery_workspaces').fetchone()
            assert row[0] == str(latest)
        assert restored.checkpoints.identity()['recovery_held']
        assert not (restored.root / 'codex-runtime/instance').exists()
        return
    prepared_workspace = restored.checkpoints.prepare_restore_workspace(**request)
    assert prepared_workspace == {'format_version': '1', 'state': 'workspace_prepared',
        'instance_id': 'instance', 'checkpoint_id': checkpoint['checkpoint_id'],
        'store_generation': binding['store_generation']}
    assert restored.checkpoints.prepare_restore_workspace(**request) == prepared_workspace
    assert checkpoint == original
    assert restored.checkpoints.identity()['recovery_held']
    with restored.store.connect() as db:
        row = db.execute('SELECT source_path,target_path FROM recovery_workspaces').fetchone()
        assert tuple(row) == (origin, str(destination))
        assert db.execute('SELECT cwd FROM sessions').fetchone()[0] == str(destination)
    bad = deepcopy(request)
    bad['params']['checkpoint']['owner_ref'] = 'different-owner'
    with pytest.raises(BridgeError) as caught:
        restored.checkpoints.prepare_restore_workspace(**bad)
    assert caught.value.code == 'scope_mismatch'
    result = restored.checkpoints.restore(**request)
    assert result['state'] == 'restored_held'
    assert restored.checkpoints.restore(**request) == result
    with closing(sqlite3.connect(restored.root / 'codex-runtime/instance/state_5.sqlite')) as db:
        assert db.execute('SELECT cwd FROM threads').fetchone()[0] == str(destination)
    assert destination.stat().st_mode & 0o777 == 0o500
    restored.checkpoints.release_recovery(expected_generation=result['store_generation'])
    with pytest.raises(BridgeError) as caught:
        restored.checkpoints.prepare_restore_workspace(**request)
    assert caught.value.code == 'scope_mismatch'
