"""Identical Store semantics under SQLite and the opt-in PostgreSQL fixture plugin."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
import json
import threading
from uuid import uuid4

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.checkpoint.availability import inspect
from agentbridge.errors import BridgeError
from agentbridge.proxy.credential_barrier import initialize, supervisor_hold
from fixtures.test_checkpoint_fixture import execution, prepared, retry, scope, terminal
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


def test_committed_event_cursor_never_skips_a_delayed_writer(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    other = Bridge(tmp_path / 'independent')
    entered, release = threading.Event(), threading.Event()
    later_entered = threading.Event()

    def delayed():
        with bridge.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT INTO events(run_id,session_id,kind,at,data) VALUES (?,?,?,?,?)',
                               ('turn', 'instance', 'first', 1., '{}'))
            entered.set()
            assert release.wait(5)

    def later():
        later_entered.set()
        with bridge.store.connect() as connection:
            connection.execute('INSERT INTO events(run_id,session_id,kind,at,data) VALUES (?,?,?,?,?)',
                               ('turn', 'instance', 'second', 2., '{}'))

    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(delayed)
        assert entered.wait(3)
        second = executor.submit(later)
        # An unrelated Store remains usable while this Store holds its writer barrier.
        with other.store.connect() as connection:
            assert connection.execute('SELECT version FROM metadata').fetchone()[0] == 16
        assert later_entered.wait(3)
        with pytest.raises(TimeoutError):
            second.result(timeout=.1)
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    events = bridge.store.events(run_id='turn')
    assert [event.kind for event in events] == ['first', 'second']
    assert events[0].seq < events[1].seq
    assert [event.kind for event in bridge.store.events(run_id='turn', after=events[0].seq)] == ['second']


def test_rollback_does_not_publish_event_or_lose_next_cursor(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    with pytest.raises(RuntimeError):
        with bridge.store.connect() as connection:
            connection.execute('INSERT INTO events(run_id,session_id,kind,at,data) VALUES (?,?,?,?,?)',
                               ('turn', 'instance', 'rolled_back', 1., '{}'))
            raise RuntimeError('fixture interruption')
    assert bridge.store.events(run_id='turn') == []
    with bridge.store.connect() as connection:
        connection.execute('INSERT INTO events(run_id,session_id,kind,at,data) VALUES (?,?,?,?,?)',
                           ('turn', 'instance', 'committed', 2., '{}'))
    assert [event.kind for event in Bridge(bridge.root).store.events(run_id='turn')] == ['committed']


def test_parallel_idempotent_admission_and_account_barrier(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'fixture', 8371, model='fixture-model')
    bridge.store.add_session('instance', 'fixture', str(tmp_path), 'fixture-model')
    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(lambda index: bridge.store.admit(
            'turn-' + str(index), 'instance', 'once', RunOptions(model='fixture-model'), 'same'),
            range(4)))
    assert len({result[0] for result in results}) == 1
    assert sum(result[1] for result in results) == 1
    with bridge.store.connect() as connection:
        initialize(connection)
        connection.execute('INSERT INTO credential_account_holds VALUES (?,?,?)',
                           ('fixture', 'operation', 'capture'))
    assert tuple(supervisor_hold(bridge.root, 'fixture')) == ('operation', 'capture')
    with pytest.raises(BridgeError) as blocked:
        bridge.store.admit('extra', 'instance', 'blocked', RunOptions(model='fixture-model'), 'extra')
    assert blocked.value.code in {'credential_snapshot_pending', 'busy'}


def test_native_checkpoint_observation_and_restore_remain_portable(tmp_path):
    bridge = prepared(tmp_path)
    home = execution(bridge)
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    checkpoint = retry(bridge)
    observed = bridge.checkpoints.observe_store(format_version='1', params=scope(bridge))
    assert observed['backend'] == bridge.store.storage.name
    assert observed['coverage'][0]['checkpoint_id'] == checkpoint['checkpoint_id']
    assert observed['cursor']['seq'] > terminal(bridge).seq
    assert inspect(bridge.root) == {'state': 'present'}
    if bridge.store.storage.name == 'postgresql':
        assert observed['sql_position']['boundary'] == 'next_record_exclusive'
        with pytest.raises(BridgeError) as required:
            bridge.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                               params=scope(bridge))
        assert required.value.code == 'checkpoint_sql_backup_required'
        assert not (bridge.root / 'bridge.sqlite3').exists()
    # Simulate the host's already recovered SQL generation. The native restore is real;
    # this fixture does not claim a PostgreSQL physical backup or migration proof.
    source_generation = observed['store_generation']
    generation = str(uuid4())
    with bridge.store.connect() as connection:
        connection.execute('UPDATE store_identity SET store_generation=?,source_generation=?, '
                           'source_seq=?,recovery_held=1 WHERE singleton=1',
                           (generation, source_generation, observed['cursor']['seq']))
    home.rename(tmp_path / 'offline-native-origin')
    operation = str(uuid4())
    params = {'checkpoint': checkpoint, 'destination_generation': generation,
              'content': checkpoint['content']}
    result = bridge.checkpoints.restore(format_version='1', operation_id=operation, params=params)
    assert result['state'] == 'restored_held'
    assert bridge.checkpoints.restore(format_version='1', operation_id=operation, params=params) == result
    assert (home / 'state_5.sqlite').is_file()
    bridge.checkpoints.release_recovery(expected_generation=generation)
    assert bridge.checkpoints.identity()['recovery_held'] is False
    with pytest.raises(BridgeError):
        bridge.checkpoints.observe_store(format_version='1', params={**scope(bridge), 'owner_ref': 'other'})
    assert 'conninfo' not in json.dumps(observed)
