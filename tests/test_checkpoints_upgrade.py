"""Legacy history stays fenced until a proven native seal and host SQL reconciliation."""

from copy import deepcopy
from uuid import uuid4

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.checkpoint.snapshot import restore_store
from agentbridge.errors import BridgeError
from agentbridge.rpc import dispatch
from fixtures.test_checkpoint_fixture import execution, prepared, scope, terminal


def legacy(tmp_path):
    bridge = prepared(tmp_path, durable=False)
    execution(bridge)
    bridge.store.finish('turn-1', 'completed')
    expected = {'turn_id': 'turn-1', 'native_id': bridge.get_session('instance')['native_id'],
                'after_seq': terminal(bridge).seq}
    return bridge, expected, str(uuid4()), str(uuid4())


def begin(bridge, operation_id, proof_ref, verify=lambda _: True):
    return bridge.checkpoints.begin_upgrade(operation_id=operation_id, owner_ref='fixture-owner',
                                           proof_ref=proof_ref, verify_quiescence=verify)


def blocked(bridge):
    with pytest.raises(BridgeError) as caught:
        bridge.store.admit('next', 'instance', 'next', RunOptions(), 'next')
    assert caught.value.code == 'checkpoint_pending'


def test_upgrade_holds_before_enable_and_requires_reconciled_commit(tmp_path):
    bridge, expected, operation_id, proof_ref = legacy(tmp_path)
    original_terminal = deepcopy(terminal(bridge).data)
    identity = bridge.checkpoints.identity()
    with pytest.raises(BridgeError):
        begin(bridge, operation_id, proof_ref, lambda _: False)
    restarted = Bridge(bridge.root)
    assert not restarted.checkpoints.identity()['enabled']
    blocked(restarted)
    with pytest.raises(BridgeError):
        begin(restarted, str(uuid4()), proof_ref)
    begin(restarted, operation_id, proof_ref)
    result = restarted.checkpoints.stage_upgrade('instance', operation_id=operation_id,
        expected=expected, verify_quiescence=lambda _: True)
    assert result['cursor'] == identity['cursor']
    assert result['checkpoint']['native_id'] == expected['native_id']
    assert result['terminal_seq'] == expected['after_seq']
    assert result['checkpoint_event_seq'] > result['terminal_seq']
    blocked(restarted)
    assert terminal(restarted).data == original_terminal
    assert restarted.checkpoints.stage_upgrade('instance', operation_id=operation_id,
        expected=expected, verify_quiescence=lambda _: False) == result
    reconciliation_ref = str(uuid4())
    with pytest.raises(BridgeError):
        restarted.checkpoints.confirm_upgrade('instance', operation_id=operation_id,
            reconciliation_ref=reconciliation_ref, verify_reconciliation=lambda _: False)
    blocked(restarted)
    observed = []
    def committed(value):
        observed.append(value)
        return value == {**result, 'reconciliation_ref': reconciliation_ref}
    assert restarted.checkpoints.confirm_upgrade('instance', operation_id=operation_id,
        reconciliation_ref=reconciliation_ref, verify_reconciliation=committed) == result
    # A lost successful response can be retried; a different commit proof is not accepted.
    assert restarted.checkpoints.confirm_upgrade('instance', operation_id=operation_id,
        reconciliation_ref=reconciliation_ref, verify_reconciliation=lambda _: False) == result
    with pytest.raises(BridgeError):
        restarted.checkpoints.confirm_upgrade('instance', operation_id=operation_id,
            reconciliation_ref=str(uuid4()), verify_reconciliation=lambda _: True)
    assert len(observed) == 1
    assert restarted.store.admit('next', 'instance', 'next', RunOptions(), 'next')[1]


@pytest.mark.parametrize('field,value', [('native_id', str(uuid4())), ('turn_id', 'wrong'),
                                        ('after_seq', 10000), ('after_seq', 0)])
def test_discrepant_or_unconsumed_history_stays_held(tmp_path, field, value):
    bridge, expected, operation_id, proof_ref = legacy(tmp_path)
    begin(bridge, operation_id, proof_ref)
    with pytest.raises(BridgeError):
        bridge.checkpoints.stage_upgrade('instance', operation_id=operation_id,
            expected={**expected, field: value}, verify_quiescence=lambda _: True)
    blocked(Bridge(bridge.root))
    assert 'durability' not in terminal(bridge).data


def test_other_instances_and_restored_upgrade_authority_remain_held(tmp_path):
    bridge, expected, operation_id, proof_ref = legacy(tmp_path)
    bridge.store.add_session('other', 'fixture', str(tmp_path), 'fixture-model')
    begin(bridge, operation_id, proof_ref)
    result = bridge.checkpoints.stage_upgrade('instance', operation_id=operation_id,
        expected=expected, verify_quiescence=lambda _: True)
    bridge.checkpoints.confirm_upgrade('instance', operation_id=operation_id,
        reconciliation_ref=str(uuid4()), verify_reconciliation=lambda _: True)
    assert bridge.checkpoints.identity()['continuity_holds'] == [
        {'instance_id': 'other', 'reason': 'legacy_upgrade:' + operation_id}]
    with pytest.raises(BridgeError):
        bridge.store.admit('other-turn', 'other', 'next', RunOptions(), 'other-turn')
    snapshot = bridge.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                params=scope(bridge))
    assert snapshot['store_schema'] == 15
    restored_scope = restore_store(tmp_path / 'restored', snapshot,
        bridge.checkpoints.resolve_content(snapshot['content']), owner_ref='fixture-owner')
    restored = Bridge(tmp_path / 'restored')
    assert restored_scope['store_generation'] != result['cursor']['store_generation']
    with pytest.raises(BridgeError):
        begin(restored, operation_id, proof_ref)
    with pytest.raises(BridgeError):
        restored.checkpoints.confirm_upgrade('instance', operation_id=operation_id,
            reconciliation_ref=str(uuid4()), verify_reconciliation=lambda _: True)
    with pytest.raises(BridgeError):
        dispatch(bridge, 'checkpoints.begin_upgrade', {'operation_id': operation_id})


def test_schema14_upgrade_preserves_store_identity_and_integer_position(tmp_path):
    bridge, expected, _, _ = legacy(tmp_path)
    before = bridge.checkpoints.identity()
    with bridge.store.connect() as db:
        db.execute('DROP TABLE checkpoint_upgrade_instances')
        db.execute('DROP TABLE checkpoint_upgrade_operations')
        db.execute('UPDATE metadata SET version=14')
    upgraded = Bridge(bridge.root)
    assert upgraded.checkpoints.identity() == before
    assert terminal(upgraded).seq == expected['after_seq']
    with upgraded.store.connect() as db:
        assert db.execute('SELECT version FROM metadata').fetchone()[0] == 15


def test_restore_schema14_is_explicitly_migrated_and_never_carries_upgrade_proof(tmp_path):
    bridge = prepared(tmp_path)
    with bridge.store.connect() as db:
        db.execute('DROP TABLE checkpoint_upgrade_instances')
        db.execute('DROP TABLE checkpoint_upgrade_operations')
        db.execute('UPDATE metadata SET version=14')
    snapshot = bridge.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                params=scope(bridge))
    assert snapshot['store_schema'] == 14
    restored_scope = restore_store(tmp_path / 'restored', snapshot,
        bridge.checkpoints.resolve_content(snapshot['content']), owner_ref='fixture-owner')
    restored = Bridge(tmp_path / 'restored')
    assert restored.checkpoints.identity()['recovery_held']
    assert restored_scope['store_generation'] != snapshot['store_generation']
    with restored.store.connect() as db:
        assert db.execute('SELECT version FROM metadata').fetchone()[0] == 15
        assert db.execute('SELECT COUNT(*) FROM checkpoint_upgrade_operations').fetchone()[0] == 0
