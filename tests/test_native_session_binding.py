"""Native conversation identity survives migration and rejects replacement."""

import pytest

from agentbridge.codex_control import CodexControl
from agentbridge.errors import BridgeError
from agentbridge.models import RunOptions
from agentbridge.native_sessions import mark_native_launch, migrate_v12, require_native_session
from agentbridge.store import Store
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


def prepared(tmp_path):
    store = Store(tmp_path / 'state')
    register_verified_proxy_account(store, 'fixture', 8301, model='fixture-model')
    store.add_session('instance', 'fixture', str(tmp_path), 'fixture-model')
    store.admit('first', 'instance', 'fixture input', RunOptions(), None)
    store.emit('first', 'session', {'native_id': 'native-original'})
    return store


@pytest.mark.parametrize('replacement', [None, 'native-replacement'])
def test_explicit_update_cannot_replace_or_clear_native_identity(tmp_path, replacement):
    store = prepared(tmp_path)
    store.finish('first', 'completed')
    with pytest.raises(BridgeError, match='native conversation') as caught:
        store.update_session('instance', native_id=replacement)
    assert caught.value.code == 'native_session_diverged'
    assert store.get('sessions', 'instance')['native_id'] == 'native-original'


def test_rejected_native_observation_does_not_publish_or_overwrite_identity(tmp_path):
    store = prepared(tmp_path)
    before = store.events(session_id='instance')
    with pytest.raises(BridgeError) as caught:
        store.emit('first', 'session', {'native_id': 'native-replacement'})
    assert caught.value.code == 'native_session_diverged'
    assert store.events(session_id='instance') == before
    assert store.get('sessions', 'instance')['native_id'] == 'native-original'


@pytest.mark.parametrize('native_id', [None, '', 17])
def test_missing_native_observation_is_explicit_failure(tmp_path, native_id):
    store = prepared(tmp_path)
    with pytest.raises(BridgeError) as caught:
        store.emit('first', 'session', {'native_id': native_id})
    assert caught.value.code == 'native_session_missing'
    assert store.get('sessions', 'instance')['native_id'] == 'native-original'


@pytest.mark.parametrize('current', ['native-original', None])
def test_v12_anchors_current_history_or_last_observation_after_legacy_clear(tmp_path, current):
    store = prepared(tmp_path)
    store.finish('first', 'failed')
    with store.connect() as db:
        db.execute('DROP TABLE native_session_bindings')
        db.execute('UPDATE sessions SET native_id=? WHERE id=?', (current, 'instance'))
        db.execute("UPDATE session_routing SET last_native_id='native-older'")
        db.execute('UPDATE metadata SET version=11')
    migrated = Store(store.root)
    expected = current or 'native-original'
    assert migrated.get('sessions', 'instance')['native_id'] == expected
    with migrated.connect() as db:
        binding = db.execute('SELECT native_id FROM native_session_bindings').fetchone()
        assert binding['native_id'] == expected
        assert require_native_session(db, migrated.get('sessions', 'instance')) == expected
    assert [event.data['native_id'] for event in migrated.events(session_id='instance')
            if event.kind == 'session'] == ['native-original']


def test_v12_rechecks_version_under_write_lock(tmp_path):
    store = prepared(tmp_path)
    with store.connect() as db:
        assert migrate_v12(db, 11) == 13
        assert db.execute('SELECT count(*) FROM native_session_bindings').fetchone()[0] == 1


def test_legacy_rollback_to_older_thread_does_not_silently_omit_later_history(tmp_path):
    store = prepared(tmp_path)
    store.finish('first', 'completed')
    store.admit('second', 'instance', 'later work', RunOptions(), None)
    with store.connect() as db:
        store._event(db, 'second', 'instance', 'session', {'native_id': 'native-later'})
        db.execute('DROP TABLE native_session_bindings')
        db.execute('UPDATE metadata SET version=11')
    store.finish('second', 'failed')
    migrated = Store(store.root)
    assert migrated.get('sessions', 'instance')['native_id'] == 'native-original'
    with migrated.connect() as db, pytest.raises(BridgeError) as caught:
        require_native_session(db, migrated.get('sessions', 'instance'))
    assert caught.value.code == 'native_session_diverged'
    assert len(migrated.session_runs('instance')) == 2
    assert [event.data['native_id'] for event in migrated.events(session_id='instance')
            if event.kind == 'session'] == ['native-original', 'native-later']


def test_deleting_instance_removes_its_native_binding(tmp_path):
    store = prepared(tmp_path)
    store.finish('first', 'completed')
    assert store.delete_instance('instance')['deleted'] is True
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM native_session_bindings').fetchone()[0] == 0


def test_unidentified_launched_history_cannot_be_replaced(tmp_path):
    store = Store(tmp_path / 'state')
    register_verified_proxy_account(store, 'fixture', 8301, model='fixture-model')
    store.add_session('instance', 'fixture', str(tmp_path), 'fixture-model')
    store.admit('first', 'instance', 'fixture input', RunOptions(), None)
    store.emit('first', 'run_started', {'engine': 'codex'})
    store.finish('first', 'interrupted', 'worker_lost')
    with store.connect() as db, pytest.raises(BridgeError) as caught:
        require_native_session(db, store.get('sessions', 'instance'))
    assert caught.value.code == 'native_session_missing'


@pytest.mark.parametrize('evidence', ['child_pid', 'launch_intent'])
def test_crash_before_first_native_event_cannot_start_a_second_session(tmp_path, evidence):
    store = Store(tmp_path / 'state')
    register_verified_proxy_account(store, 'fixture', 8301, model='fixture-model')
    store.add_session('instance', 'fixture', str(tmp_path), 'fixture-model')
    store.admit('first', 'instance', 'fixture input', RunOptions(), None)
    if evidence == 'child_pid':
        store.update('first', child_pid=987654321)
    else:
        mark_native_launch(store, 'first')
    store.finish('first', 'interrupted', 'worker_lost')
    with store.connect() as db, pytest.raises(BridgeError) as caught:
        require_native_session(db, store.get('sessions', 'instance'))
    assert caught.value.code == 'native_session_missing'


def test_definite_process_launch_failure_can_start_the_first_native_session(tmp_path):
    store = Store(tmp_path / 'state')
    register_verified_proxy_account(store, 'fixture', 8301, model='fixture-model')
    store.add_session('instance', 'fixture', str(tmp_path), 'fixture-model')
    store.admit('first', 'instance', 'fixture input', RunOptions(), None)
    mark_native_launch(store, 'first')
    mark_native_launch(store, 'first', not_started=True)
    store.finish('first', 'failed', 'provider_unavailable')
    with store.connect() as db:
        assert require_native_session(db, store.get('sessions', 'instance')) is None


def test_malformed_legacy_native_id_cannot_be_treated_as_a_new_chat(tmp_path):
    store = prepared(tmp_path)
    store.finish('first', 'completed')
    with store.connect() as db:
        db.execute('DROP TABLE native_session_bindings')
        db.execute("UPDATE sessions SET native_id='' WHERE id='instance'")
        db.execute('UPDATE metadata SET version=11')
    migrated = Store(store.root)
    with migrated.connect() as db, pytest.raises(BridgeError) as caught:
        require_native_session(db, migrated.get('sessions', 'instance'))
    assert caught.value.code == 'native_session_missing'


@pytest.mark.parametrize('returned_id', ['native-replacement', None, ''])
def test_app_server_rejects_wrong_resume_before_starting_a_turn(returned_id):
    requests, observations = [], []
    class Channel:
        def send(self, value):
            requests.append(value)
        def receive(self, timeout):
            request = requests[-1]
            return {'id': request['id'], 'result': (
                {} if request['method'] == 'initialize' else {'thread': {'id': returned_id}})}

    payload = {'native_id': 'native-original', 'model': 'changed-model', 'cwd': '/fixture',
               'options': {'permission_mode': 'dontAsk', 'sandbox': 'read-only', 'timeout': 1}}
    control = CodexControl(Channel(), payload, observations.append, None)
    with pytest.raises(BridgeError) as caught:
        control.execute()
    assert caught.value.code == ('native_session_diverged' if returned_id else 'native_session_missing')
    assert [request['method'] for request in requests] == ['initialize', 'initialized', 'thread/resume']
    assert requests[-1]['params']['threadId'] == 'native-original'
    assert requests[-1]['params']['model'] == 'changed-model'
    assert observations == []
