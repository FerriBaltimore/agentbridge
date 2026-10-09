"""Native chat keeps progressing while explicit backup retains exact stopped-turn evidence."""

from copy import deepcopy
from uuid import uuid4

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.checkpoint import native
from agentbridge.checkpoint.snapshot import restore_store
from agentbridge.cli import open_bridge
from agentbridge.commands.parser import build_parser
from agentbridge.errors import BridgeError
from agentbridge.native_sessions import mark_native_launch
from agentbridge.proxy import credential_barrier
from fixtures.test_checkpoint_fixture import execution, prepared, retry, scope, terminal


def deferred(tmp_path):
    bridge = prepared(tmp_path)
    return Bridge(bridge.root, checkpoint_mode='on_demand')


def snapshot(bridge):
    return bridge.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                           params=scope(bridge))


def test_capture_failure_cannot_block_next_message_or_claim_newer_native_bytes(tmp_path, monkeypatch):
    bridge = deferred(tmp_path)
    home = execution(bridge)
    copied = []

    def unavailable(*args):
        copied.append(args)
        native.fail('checkpoint_incomplete')

    monkeypatch.setattr(native, 'materialize', unavailable)
    bridge.store.finish('turn-1', 'interrupted', 'unknown_outcome', process_verified=True)
    original = deepcopy(terminal(bridge).data)
    assert copied == []
    assert original['durability']['state'] == 'pending'
    with pytest.raises(BridgeError, match='unverified'):
        retry(bridge)
    assert len(copied) == 1
    assert snapshot(bridge)['coverage'] == []
    restarted = Bridge(bridge.root)
    assert restarted.checkpoints.identity()['checkpoint_mode'] == 'on_demand'
    restarted.instance_update('instance', sandbox_mode='workspace-write')
    assert restarted.store.admit('turn-2', 'instance', 'independent', RunOptions(), 'next')[1]
    # Lost acknowledgement never makes the first external operation eligible for replay.
    assert restarted.store.admit('ignored', 'instance', 'hello',
                                RunOptions(model='fixture-model'), 'turn-1') == ('turn-1', False)
    mark_native_launch(restarted.store, 'turn-2')
    rollout = next((home / 'sessions').glob('*.jsonl'))
    with rollout.open('a') as handle:
        handle.write('{"type":"message","text":"newer native state"}\n')
    restarted.store.emit('turn-2', 'assistant', {'text': 'next answer'})
    restarted.store.finish('turn-2', 'completed', process_verified=True)
    monkeypatch.undo()
    with pytest.raises(BridgeError) as stale:
        retry(restarted)
    assert stale.value.code == 'checkpoint_incomplete'
    current = retry(restarted, turn='turn-2')
    assert current['turn_id'] == 'turn-2'
    assert [item['turn_id'] for item in snapshot(restarted)['coverage']] == ['turn-2']
    assert terminal(restarted).data == original
    with restarted.store.connect() as db:
        first = db.execute("SELECT state,descriptor FROM native_checkpoints WHERE turn_id='turn-1'")
        assert tuple(first.fetchone()) == ('pending', None)


def test_explicit_capture_locks_admission_and_restores_held_without_replaying(tmp_path, monkeypatch):
    bridge = deferred(tmp_path)
    execution(bridge)
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    original = deepcopy(terminal(bridge).data)
    materialize = native.materialize
    checked = []

    def copying(home, stage):
        mutations = (
            lambda: bridge.store.admit('raced', 'instance', 'raced', RunOptions(), 'raced'),
            lambda: bridge.instance_update('instance', sandbox_mode='workspace-write'),
            lambda: bridge.instance_delete('instance'),
        )
        for mutation in mutations:
            with pytest.raises(BridgeError) as locked:
                mutation()
            assert locked.value.code == 'busy'
        checked.append(True)
        return materialize(home, stage)

    monkeypatch.setattr(native, 'materialize', copying)
    checkpoint = retry(bridge)
    assert checked == [True]
    assert retry(bridge) == checkpoint
    stored = snapshot(bridge)
    assert stored['coverage'][0]['checkpoint_id'] == checkpoint['checkpoint_id']
    assert terminal(bridge).data == original
    result = restore_store(tmp_path / 'restored', stored,
                           bridge.checkpoints.resolve_content(stored['content']),
                           owner_ref='fixture-owner')
    restored = Bridge(tmp_path / 'restored')
    assert restored.checkpoints.identity()['checkpoint_mode'] == 'on_demand'
    with pytest.raises(BridgeError) as held:
        restored.instance_update('instance', sandbox_mode='workspace-write')
    assert held.value.code == 'checkpoint_pending'
    restored.checkpoints.register_content(checkpoint['content'],
                                         bridge.checkpoints.resolve_content(checkpoint['content']))
    restored.checkpoints.restore(format_version='1', operation_id=str(uuid4()), params={
        'checkpoint': checkpoint, 'content': checkpoint['content'],
        'destination_generation': result['store_generation']})
    restored.checkpoints.release_recovery(expected_generation=result['store_generation'])
    assert restored.store.admit('next', 'instance', 'next', RunOptions(), 'next')[1]
    assert restored.store.admit('ignored', 'instance', 'hello',
                               RunOptions(model='fixture-model'), 'turn-1') == ('turn-1', False)
    assert snapshot(restored)['coverage'] == []
    assert bridge.instance_update('instance', sandbox_mode='workspace-write')['sandbox_mode'] == (
        'workspace-write')
    assert bridge.instance_delete('instance')['deleted']


def test_unverified_process_and_credential_or_continuity_holds_still_block(tmp_path, monkeypatch):
    bridge = deferred(tmp_path)
    execution(bridge)
    bridge.store.finish('turn-1', 'interrupted', 'unknown_outcome', process_verified=False)
    with pytest.raises(BridgeError) as pending:
        bridge.instance_update('instance', sandbox_mode='workspace-write')
    assert pending.value.code == 'checkpoint_pending'
    operation = terminal(bridge).data['durability']['barrier_id']
    monkeypatch.setattr(native, 'materialize', lambda *_: native.fail('checkpoint_incomplete'))
    observed = []

    def drained(binding):
        observed.append(binding)
        return binding == {**scope(bridge), 'instance_id': 'instance',
                           'turn_id': 'turn-1', 'barrier_id': operation}

    with pytest.raises(BridgeError):
        bridge.checkpoints.adopt_drained('instance', operation_id=operation,
            proof_ref=str(uuid4()), verify_quiescence=drained)
    assert len(observed) == 1
    assert bridge.instance_update('instance', sandbox_mode='workspace-write')['sandbox_mode'] == (
        'workspace-write')
    with bridge.store.connect() as db:
        db.execute('INSERT INTO continuity_holds VALUES (?,?)', ('instance', 'fixture_restore'))
    with pytest.raises(BridgeError) as continuity:
        bridge.instance_update('instance', sandbox_mode='read-only')
    assert continuity.value.code == 'checkpoint_pending'
    with bridge.store.connect() as db:
        db.execute('DELETE FROM continuity_holds')
        credential_barrier.initialize(db)
        db.execute('INSERT INTO credential_account_holds VALUES (?,?,?)',
                   ('fixture', str(uuid4()), 'restore_pending'))
    with pytest.raises(BridgeError) as credential:
        bridge.instance_update('instance', sandbox_mode='read-only')
    assert credential.value.code == 'credential_snapshot_pending'


def test_host_policy_is_explicit_persisted_and_schema15_defaults_to_required(tmp_path):
    bridge = prepared(tmp_path)
    before = bridge.checkpoints.identity()
    with bridge.store.connect() as db:
        db.execute('ALTER TABLE store_identity DROP COLUMN checkpoint_mode')
        db.execute('UPDATE metadata SET version=15')
    reopened = Bridge(bridge.root)
    assert reopened.checkpoints.identity() == before
    parser, _ = build_parser()
    args = parser.parse_args(['--root', str(bridge.root), '--checkpoint-mode', 'on-demand',
                              'capabilities'])
    configured = open_bridge(args)
    assert configured.capabilities()['durability']['checkpoint_mode'] == 'on_demand'
    configured.store.admit('active', 'instance', 'active', RunOptions(), 'active')
    with pytest.raises(BridgeError) as busy:
        Bridge(bridge.root, checkpoint_mode='required')
    assert busy.value.code == 'checkpoint_busy'
    assert Bridge(bridge.root).checkpoints.identity()['checkpoint_mode'] == 'on_demand'
    with pytest.raises(BridgeError):
        Bridge(bridge.root, durable=False)
