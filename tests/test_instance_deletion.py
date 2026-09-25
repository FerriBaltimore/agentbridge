"""Conversation deletion purges local evidence without replay or live providers."""

import json
from pathlib import Path

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.error_learning import Learning
from agentbridge.permissions import Permissions
from agentbridge.provider_contracts import ContractRegistry
from agentbridge.proxy.home import session_home
from agentbridge.rpc import dispatch
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


MODEL = 'fixture-model'


def prepared(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'fixture', 8371, model=MODEL)
    bridge.store.add_session('conversation', 'fixture', str(tmp_path), MODEL,
                             request_key='create-conversation')
    return bridge


def error_code(error):
    return error.value.code


def test_delete_purges_conversation_and_preserves_minimal_receipt(tmp_path):
    bridge = prepared(tmp_path)
    store = bridge.store
    store.add_session('transferred-child', 'fixture', str(tmp_path), MODEL,
                      parent_id='conversation', context='portable child context')
    store.admit('turn-1', 'conversation', 'private prompt 57230',
                RunOptions(model=MODEL), 'message-key')
    store.emit('turn-1', 'message', {'content': 'private answer 57230'})
    store.finish('turn-1', 'completed')
    home = tmp_path / 'state' / 'codex-runtime' / 'conversation'
    home_path = session_home(store.root, 'conversation')
    assert str(home) == home_path
    (home / 'native.jsonl').write_text('private native content 57230')
    archive = Path(bridge.export_context('conversation').archive_path)
    sibling_archive = bridge.root / 'archives' / 'conversation-extra-0123456789abcdef.jsonl'
    sibling_archive.write_text('other conversation')
    staged = bridge.root / 'archives' / '.staged-conversation~orphan'
    staged.write_text('interrupted private archive')
    Permissions(store)
    ContractRegistry(store)
    Learning(store)
    with store.connect() as db:
        db.execute('INSERT INTO run_route_exclusions(run_id,refs) VALUES (?,?)',
                   ('turn-1', '["fixture"]'))
        db.execute('INSERT INTO permission_requests(id,run_id,request,expires) VALUES (?,?,?,?)',
                   ('permission-1', 'turn-1', '{}', 0))
        db.execute('INSERT INTO run_contracts(run_id,data) VALUES (?,?)',
                   ('turn-1', '{}'))
        db.execute('INSERT INTO error_cases VALUES (?,?,?,?,?,1,?,?,?,?)',
                   ('case-1', 'codex', '', 'fixture-fingerprint', '{}', 0, 0,
                    'turn-1', 'turn-1'))

    result = dispatch(bridge, 'instances.delete', {'instance_id': 'conversation'})
    assert result == {'instance_id': 'conversation', 'deleted': True, 'pending': False}
    assert bridge.instance_delete('conversation') == result
    assert not home.exists()
    assert not archive.exists()
    assert not staged.exists()
    assert sibling_archive.read_text() == 'other conversation'
    assert [row['id'] for row in bridge.sessions()] == ['transferred-child']
    assert store.get('sessions', 'transferred-child')['parent_id'] is None
    assert bridge.runs() == []
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events WHERE session_id=?',
                          ('conversation',)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM run_route_exclusions WHERE run_id=?',
                          ('turn-1',)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM permission_requests WHERE run_id=?',
                          ('turn-1',)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM run_contracts WHERE run_id=?',
                          ('turn-1',)).fetchone()[0] == 0
        case = db.execute('SELECT first_turn_id,last_turn_id FROM error_cases WHERE id=?',
                          ('case-1',)).fetchone()
        assert case['first_turn_id'] is None and case['last_turn_id'] is None
        receipt = db.execute('SELECT status,deleted_at FROM deleted_instances WHERE session_id=?',
                             ('conversation',)).fetchone()
        assert receipt['status'] == 'deleted' and receipt['deleted_at'] is not None
        request = db.execute('SELECT payload FROM instance_requests WHERE session_id=?',
                             ('conversation',)).fetchone()
        assert json.loads(request['payload']) == {'instance_deleted': True}
    assert b'private prompt 57230' not in store.path.read_bytes()
    with pytest.raises(BridgeError) as error:
        bridge.instance_get('conversation')
    assert error_code(error) == 'not_found'
    with pytest.raises(BridgeError) as error:
        store.replay_pinned_session('create-conversation', 'fixture', str(tmp_path), MODEL)
    assert error_code(error) == 'instance_deleted'
    with pytest.raises(BridgeError) as error:
        store.add_session('conversation', 'fixture', str(tmp_path), MODEL)
    assert error_code(error) == 'instance_deleted'
    assert bridge.capabilities()['operations']['instances.delete']['maturity'] == 'fixture_tested'


def test_delete_rejects_active_turn_and_unidentified_owned_process(tmp_path):
    bridge = prepared(tmp_path)
    bridge.store.admit('turn-1', 'conversation', 'work', RunOptions(model=MODEL), None)
    with pytest.raises(BridgeError) as error:
        bridge.instance_delete('conversation')
    assert error_code(error) == 'busy'
    assert bridge.instance_get('conversation')['state'] == 'active'
    with bridge.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM deleted_instances').fetchone()[0] == 0
    bridge.store.finish('turn-1', 'interrupted', 'worker_lost')
    bridge.store.update('turn-1', child_pid=12345, child_identity=None)
    with pytest.raises(BridgeError) as error:
        bridge.instance_delete('conversation')
    assert error_code(error) == 'busy'
    bridge.store.update('turn-1', child_pid=None, child_identity=None)
    assert bridge.instance_delete('conversation')['deleted'] is True


def test_delete_retries_after_home_cleanup_failure(tmp_path, monkeypatch):
    bridge = prepared(tmp_path)
    home = tmp_path / 'state' / 'codex-runtime' / 'conversation'
    session_home(bridge.root, 'conversation')
    (home / 'native.jsonl').write_text('private state')
    from agentbridge import instance_deletion

    original = instance_deletion.remove_session_home
    attempts = []

    def fail_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise BridgeError('instance_cleanup_failed', 'Retry deletion.')
        return original(*args, **kwargs)

    monkeypatch.setattr(instance_deletion, 'remove_session_home', fail_once)
    with pytest.raises(BridgeError) as error:
        bridge.instance_delete('conversation')
    assert error_code(error) == 'instance_cleanup_failed'
    assert home.exists()
    with bridge.store.connect() as db:
        assert db.execute('SELECT status FROM deleted_instances WHERE session_id=?',
                          ('conversation',)).fetchone()['status'] == 'deleting'
    assert bridge.instance_delete('conversation')['deleted'] is True
    assert not home.exists()


def test_reopening_finishes_a_committed_deletion(tmp_path, monkeypatch):
    bridge = prepared(tmp_path)
    from agentbridge import instance_deletion

    original = instance_deletion.remove_session_home

    def fail_cleanup(*_args, **_kwargs):
        raise BridgeError('instance_cleanup_failed', 'Retry deletion.')

    monkeypatch.setattr(instance_deletion, 'remove_session_home', fail_cleanup)
    with pytest.raises(BridgeError):
        bridge.instance_delete('conversation')
    monkeypatch.setattr(instance_deletion, 'remove_session_home', original)
    reopened = Bridge(tmp_path / 'state')
    with reopened.store.connect() as db:
        assert db.execute('SELECT status FROM deleted_instances WHERE session_id=?',
                          ('conversation',)).fetchone()['status'] == 'deleted'


def test_export_finishing_after_delete_does_not_leave_an_archive(tmp_path, monkeypatch):
    bridge = prepared(tmp_path)
    bridge.store.admit('turn-1', 'conversation', 'private prompt',
                       RunOptions(model=MODEL), None)
    bridge.store.finish('turn-1', 'completed')
    from agentbridge import continuity

    original = continuity.atomic_private

    def delete_before_archive_write(*args, **kwargs):
        assert bridge.instance_delete('conversation')['deleted'] is True
        return original(*args, **kwargs)

    monkeypatch.setattr(continuity, 'atomic_private', delete_before_archive_write)
    with pytest.raises(BridgeError) as error:
        bridge.export_context('conversation')
    assert error_code(error) == 'not_found'
    assert list((bridge.root / 'archives').iterdir()) == []


def test_delete_checks_version_and_requires_evaluation_discard(tmp_path):
    bridge = prepared(tmp_path)
    with pytest.raises(BridgeError) as error:
        bridge.instance_delete('conversation', expected_version=2)
    assert error_code(error) == 'version_conflict'
    bridge.store.add_session('evaluation', 'fixture', str(tmp_path), MODEL,
                             evaluation=True)
    with pytest.raises(BridgeError) as error:
        bridge.instance_delete('evaluation')
    assert error_code(error) == 'evaluation_discard_required'
    assert bridge.instance_get('conversation')['state'] == 'active'
