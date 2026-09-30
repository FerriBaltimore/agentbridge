"""Restored SQL changes host authority exactly once and retains its source replay floor."""

import os
from uuid import uuid4

import pytest

from agentbridge import Bridge
from agentbridge.checkpoint import restore_postgres
from agentbridge.errors import BridgeError
from fixtures.test_checkpoint_fixture import execution, prepared, retry, scope

pytestmark = pytest.mark.skipif(not os.environ.get('AGENTBRIDGE_TEST_POSTGRES_SOCKET'),
                                reason='Owned PostgreSQL fixture is not selected.')


def recovered_store(tmp_path):
    # The shared fixture provisions a fresh isolated PG schema. The following is the
    # recovered content fixture, not evidence of a physical PostgreSQL restore.
    bridge = prepared(tmp_path)
    execution(bridge)
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    retry(bridge)
    snapshot = bridge.checkpoints.observe_store(format_version='1', params=scope(bridge))
    snapshot.pop('sql_position')
    snapshot.update(sql_frontier_id=str(uuid4()), snapshot_id=str(uuid4()),
                    created_at='2026-09-30T00:00:00Z')
    return bridge, snapshot


def test_destination_rebind_is_held_idempotent_and_rejects_old_cursor(tmp_path):
    bridge, snapshot = recovered_store(tmp_path)
    operation = str(uuid4())
    destination = tmp_path / 'recovered'
    configuration = bridge.store.storage.configuration
    result = restore_postgres(destination, snapshot, postgres=configuration,
                              owner_ref='fixture-owner', operation_id=operation)
    assert result['store_generation'] != snapshot['store_generation']
    assert result['source_cursor'] == snapshot['cursor']
    assert result['replay_start']['seq'] == snapshot['cursor']['seq']
    assert result['replay_start']['store_generation'] == result['store_generation']
    assert restore_postgres(destination, snapshot, postgres=configuration,
                            owner_ref='fixture-owner', operation_id=operation) == result
    opened = Bridge(destination)
    assert opened.checkpoints.identity()['recovery_held'] is True
    assert opened.checkpoints.identity()['store_generation'] == result['store_generation']
    with opened.store.connect() as connection:
        from agentbridge.checkpoint.state import require_cursor

        with pytest.raises(BridgeError) as old:
            require_cursor(connection, snapshot['cursor'])
        assert old.value.code == 'cursor_generation_mismatch'
        assert not connection.execute('SELECT worker_pid FROM runs WHERE worker_pid IS NOT NULL').fetchone()
    with pytest.raises(BridgeError):
        restore_postgres(destination, snapshot, postgres=configuration,
                         owner_ref='fixture-owner', operation_id=str(uuid4()))


def test_restore_ack_crash_retries_receipt_and_refuses_a_later_sql_cut(tmp_path, monkeypatch):
    from agentbridge.checkpoint import postgres_restore

    bridge, snapshot = recovered_store(tmp_path)
    configuration = bridge.store.storage.configuration
    operation, destination = str(uuid4()), tmp_path / 'destination'
    save = postgres_restore.save

    def crash(root, value, **kwargs):
        if value.get('backend') == 'postgresql' and 'recovery_operation' not in value:
            raise RuntimeError('synthetic restore host crash')
        return save(root, value, **kwargs)

    monkeypatch.setattr(postgres_restore, 'save', crash)
    with pytest.raises(RuntimeError):
        restore_postgres(destination, snapshot, postgres=configuration,
                         owner_ref='fixture-owner', operation_id=operation)
    with pytest.raises(BridgeError) as pending:
        Bridge(destination)
    assert pending.value.code == 'store_recovery_pending'
    monkeypatch.setattr(postgres_restore, 'save', save)
    assert restore_postgres(destination, snapshot, postgres=configuration,
                            owner_ref='fixture-owner', operation_id=operation)['recovery_held']


def test_restore_does_not_accept_newer_cursor_or_foreign_owner(tmp_path):
    bridge, snapshot = recovered_store(tmp_path)
    configuration = bridge.store.storage.configuration
    bridge.store.emit('turn-1', 'after_observation', {})
    with pytest.raises(BridgeError) as corrupt:
        restore_postgres(tmp_path / 'later', snapshot, postgres=configuration,
                         owner_ref='fixture-owner', operation_id=str(uuid4()))
    assert corrupt.value.code == 'checkpoint_corrupt'
    assert bridge.checkpoints.identity()['store_generation'] == snapshot['store_generation']
    with pytest.raises(BridgeError):
        restore_postgres(tmp_path / 'foreign', snapshot, postgres=configuration,
                         owner_ref='another-owner', operation_id=str(uuid4()))
    assert not (tmp_path / 'foreign').exists()
