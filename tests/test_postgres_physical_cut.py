"""A physical host cut cannot wait behind a Store writer awaiting that same cut."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import threading
import time

import pytest

from agentbridge.checkpoint import observe_store
from agentbridge.checkpoint.state import identity, IDENTITY_KEYS
from agentbridge.errors import BridgeError
from agentbridge.storage.inspection import connect_selected
from agentbridge.storage.selection import save
from agentbridge.store import Store

pytestmark = pytest.mark.skipif(not os.environ.get('AGENTBRIDGE_TEST_POSTGRES_SOCKET'),
                                reason='Owned PostgreSQL fixture is not selected.')


def guarded(root, guard):
    store = Store(root, owner_ref='fixture-owner', durable=True)
    configuration = replace(store.storage.configuration, physical_guard=guard)
    save(root, {'format_version': '1', 'backend': 'postgresql', **configuration.document()})
    return Store(root)


def test_read_only_cut_finishes_with_other_store_writing_and_queued_writer(tmp_path):
    import psycopg

    guard = 88272801
    first, second = guarded(tmp_path / 'a', guard), guarded(tmp_path / 'b', guard)
    with second.connect() as connection:
        binding = identity(connection)
    scope = {key: binding[key] for key in IDENTITY_KEYS}
    stopped, started, waiting = threading.Event(), threading.Event(), threading.Event()
    counts = []

    def continuous_writer():
        while not stopped.is_set():
            with first.connect() as connection:
                connection.execute('INSERT INTO events(run_id,session_id,kind,at,data) '
                                   'VALUES (?,?,?,?,?)', ('a', 'a', 'progress', time.time(), '{}'))
            counts.append(1)
            started.set()

    def queued_writer():
        waiting.set()
        with second.connect() as connection:
            connection.execute('INSERT INTO events(run_id,session_id,kind,at,data) '
                               'VALUES (?,?,?,?,?)', ('b', 'b', 'after_cut', time.time(), '{}'))

    with ThreadPoolExecutor(max_workers=3) as pool:
        running = pool.submit(continuous_writer)
        try:
            assert started.wait(2)
            with psycopg.connect(host=os.environ['AGENTBRIDGE_TEST_POSTGRES_SOCKET'],
                                 user='postgres', dbname='postgres', autocommit=True) as host:
                host.execute("SET lock_timeout='2s'")
                host.execute('SELECT pg_advisory_lock(%s)', (guard,))
                try:
                    queued = pool.submit(queued_writer)
                    assert waiting.wait(1)
                    observed = pool.submit(observe_store, second.root,
                                           format_version='1', params=scope).result(timeout=2)
                    assert observed['cursor']['seq'] == 0
                    assert observed['sql_position']['boundary'] == 'next_record_exclusive'
                    assert not queued.done()
                finally:
                    host.execute('SELECT pg_advisory_unlock(%s)', (guard,))
            queued.result(timeout=2)
        finally:
            stopped.set()
            running.result(timeout=3)
    assert counts
    assert observe_store(second.root, format_version='1', params=scope)['cursor']['seq'] > 0


def test_observation_connection_is_read_only_after_commit_and_has_no_advisory_lock(tmp_path):
    store = guarded(tmp_path / 'state', 88272802)
    with connect_selected(store.root) as connection:
        assert connection.execute('SHOW transaction_read_only').fetchone()[0] == 'on'
        assert connection.execute('SHOW transaction_isolation').fetchone()[0] == 'repeatable read'
        assert connection.execute("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() "
                                  "AND locktype='advisory'").fetchone()[0] == 0
        connection.commit()
        with pytest.raises(BridgeError) as denied:
            connection.execute('DELETE FROM events')
        assert denied.value.details == {'sqlstate': '25006'}
        connection.rollback()
