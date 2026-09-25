"""Conversation access defaults are durable, versioned and frozen on admission."""

import json

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.execution_policy import migrate_v13
from agentbridge.queueing.persistence import QueueStore
from agentbridge.rpc import dispatch
from agentbridge.store import Store
from test_instance_routing_choices import bridge, create, completed
from test_interactive_inputs import setup_proxy
from test_execution_access import FIXTURE


@pytest.mark.parametrize('routing_mode', ['pinned', 'automatic'])
def test_create_policy_is_durable_and_part_of_creation_key(bridge, tmp_path, routing_mode):
    options = {'account_ref': 'a', 'routing_mode': routing_mode, 'permission_mode': 'default',
               'sandbox_mode': 'danger-full-access', 'idempotency_key': 'policy-create'}
    value = create(bridge, tmp_path, **options)
    restored = Bridge(bridge.root).instance_get(value['id'])
    assert (restored['permission_mode'], restored['sandbox_mode']) == ('default', 'danger-full-access')
    assert create(bridge, tmp_path, **options)['replayed']
    with pytest.raises(BridgeError) as error:
        create(bridge, tmp_path, **{**options, 'sandbox_mode': 'read-only'})
    assert error.value.code == 'idempotency_conflict'


def test_update_policy_preserves_native_identity_and_is_atomic(bridge, tmp_path):
    value = create(bridge, tmp_path, account_ref='a')
    completed(bridge, value['id'])
    updated = dispatch(bridge, 'instances.update', {'instance_id': value['id'],
        'permission_mode': 'default', 'sandbox_mode': 'workspace-write', 'expected_version': 1})
    assert updated['native_session_id'] == 'native-a'
    assert updated['version'] == 2
    assert (updated['permission_mode'], updated['sandbox_mode']) == ('default', 'workspace-write')
    for options, code in [({'sandbox_mode': 'read-only', 'expected_version': 1}, 'version_conflict'),
                          ({'sandbox_mode': 'read-only', 'expected_version': True}, 'invalid_request'),
                          ({'sandbox_mode': 'read-only', 'expected_version': 0}, 'invalid_request'),
                          ({'sandbox_mode': None, 'account_ref': 'b'}, 'invalid_sandbox'),
                          ({'permission_mode': 'plan'}, 'invalid_permissions')]:
        with pytest.raises(BridgeError) as error:
            bridge.instance_update(value['id'], **options)
        assert error.value.code == code
    assert bridge.instance_get(value['id']) == updated


@pytest.mark.parametrize('field,value,code', [
    ('permission_mode', None, 'invalid_permissions'),
    ('permission_mode', 'bypassPermissions', 'invalid_permissions'),
    ('sandbox_mode', None, 'invalid_sandbox'),
    ('sandbox_mode', 'full', 'invalid_sandbox'),
])
def test_invalid_creation_policy_does_not_create_instance(bridge, tmp_path, field, value, code):
    with pytest.raises(BridgeError) as error:
        create(bridge, tmp_path, account_ref='a', **{field: value})
    assert error.value.code == code
    assert bridge.sessions() == []


def test_message_and_legacy_submit_inherit_policy_and_explicit_override(setup_proxy, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    inside, outside = workspace / 'inside', tmp_path / 'outside'
    for path in (inside, outside):
        path.write_text('synthetic fixture')
    bridge, instance = setup_proxy(workspace_path=workspace,
        native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    bridge.instance_update(instance, sandbox_mode='workspace-write')
    prompt = json.dumps({'inside': str(inside), 'outside': str(outside)})
    first = bridge.message_create(instance, prompt, idempotency_key='message')
    run = bridge.run(first['turn_id'])
    assert run.wait(10)['state'] == 'completed'
    assert json.loads(run.text)['inside_write'] is True
    native_id = bridge.instance_get(instance)['native_session_id']
    bridge.instance_update(instance, sandbox_mode='read-only')
    assert bridge.message_create(instance, prompt, idempotency_key='message')['replayed']
    assert len(bridge.runs()) == 1
    override = bridge.message_create(instance, prompt, sandbox_mode='workspace-write')
    assert bridge.run(override['turn_id']).wait(10)['state'] == 'completed'
    assert bridge.instance_get(instance)['sandbox_mode'] == 'read-only'
    legacy = bridge.submit(instance, prompt)
    assert legacy.wait(10)['state'] == 'completed'
    assert json.loads(legacy.text)['inside_write'] is False
    assert bridge.instance_get(instance)['native_session_id'] == native_id


def test_policy_race_rejects_immediate_admission(bridge, tmp_path, monkeypatch):
    instance = create(bridge, tmp_path, account_ref='a')['id']
    submit = bridge.submit
    def raced(*args, **kwargs):
        bridge.instance_update(instance, sandbox_mode='workspace-write')
        return submit(*args, **kwargs)
    monkeypatch.setattr(bridge, 'submit', raced)
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance, 'request')
    assert error.value.code == 'version_conflict'
    assert bridge.runs() == []


def test_queue_freezes_policy_blocks_edits_and_preserves_replay(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr('agentbridge.queueing.service.request', lambda *args, **kwargs: None)
    instance = create(bridge, tmp_path, account_ref='a', sandbox_mode='workspace-write')['id']
    message = bridge.queue_add(instance, 'request', idempotency_key='queued')
    stored = QueueStore(bridge.store).get(message['message_id'])
    assert json.loads(stored['options'])['sandbox'] == 'workspace-write'
    with pytest.raises(BridgeError) as error:
        bridge.instance_update(instance, sandbox_mode='danger-full-access')
    assert error.value.code == 'busy'
    bridge.queue_delete(instance, message['message_id'])
    bridge.instance_update(instance, sandbox_mode='danger-full-access')
    assert bridge.queue_add(instance, 'request', idempotency_key='queued')['replayed']
    assert json.loads(QueueStore(bridge.store).get(message['message_id'])['options'])['sandbox'] == 'workspace-write'


def test_policy_race_rejects_queue_admission(bridge, tmp_path, monkeypatch):
    instance = create(bridge, tmp_path, account_ref='a')['id']
    add = QueueStore.add
    def raced(queue, *args, **kwargs):
        bridge.instance_update(instance, sandbox_mode='workspace-write')
        return add(queue, *args, **kwargs)
    monkeypatch.setattr(QueueStore, 'add', raced)
    with pytest.raises(BridgeError) as error:
        bridge.queue_add(instance, 'request')
    assert error.value.code == 'version_conflict'
    assert bridge.queue_list(instance)['total'] == 0


def test_v13_migration_backfills_restricted_policy_without_touching_native_session(bridge, tmp_path):
    instance = create(bridge, tmp_path, account_ref='a')['id']
    completed(bridge, instance)
    with bridge.store.connect() as db:
        db.execute('DROP TABLE instance_execution_policies')
        db.execute('UPDATE metadata SET version=12')
    restored = Store(bridge.root)
    row = restored.get('sessions', instance)
    assert row['permission_mode'] == 'dontAsk' and row['sandbox_mode'] == 'read-only'
    assert row['native_id'] == 'native-a'
    with restored.connect() as db:
        assert migrate_v13(db, 12) == 13
        assert db.execute('SELECT count(*) FROM instance_execution_policies').fetchone()[0] == 1


@pytest.mark.parametrize('evaluation', [False, True])
def test_instance_cleanup_removes_saved_policy(bridge, tmp_path, evaluation):
    instance = create(bridge, tmp_path, account_ref='a', evaluation=evaluation,
                      sandbox_mode='workspace-write')['id']
    if evaluation:
        assert bridge.instance_discard_evaluation(instance)['discarded']
    else:
        assert bridge.instance_delete(instance)['deleted']
    with bridge.store.connect() as db:
        assert db.execute('SELECT 1 FROM instance_execution_policies WHERE session_id=?',
                          (instance,)).fetchone() is None


def test_legacy_queued_request_digest_remains_replayable(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr('agentbridge.queueing.service.request', lambda *args, **kwargs: None)
    instance = create(bridge, tmp_path, account_ref='a')['id']
    observed = []
    replay = QueueStore.replay
    def capture(queue, key, digest, **kwargs):
        observed.append(kwargs['legacy_digest'])
        return replay(queue, key, digest, **kwargs)
    monkeypatch.setattr(QueueStore, 'replay', capture)
    message = bridge.queue_add(instance, 'legacy request', idempotency_key='legacy-queue')
    with bridge.store.connect() as db:
        db.execute('UPDATE queued_messages SET request_digest=? WHERE id=?',
                   (observed[0], message['message_id']))
    assert bridge.queue_add(instance, 'legacy request', idempotency_key='legacy-queue')['replayed']
    assert bridge.queue_list(instance)['total'] == 1
