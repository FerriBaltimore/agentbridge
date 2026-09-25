"""Dispatcher loss, private-input boundaries and execution admission races."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentbridge import Bridge, RunOptions, cli
from agentbridge.errors import BridgeError
from agentbridge.process import alive
from agentbridge.queueing.connection import request
from agentbridge.queueing.persistence import QueueStore
from agentbridge.store import Store
from test_interactive_inputs import context_package, setup_proxy
from test_message_queues import completed, native_history, queued, running, until


def shutdown(bridge, instance):
    with bridge.store.connect() as db:
        owner = dict(db.execute('SELECT * FROM conversation_queues WHERE session_id=?', (instance,)).fetchone())
    request(bridge.root, instance, {'action': 'shutdown'}, start=False)
    until(lambda: not alive(owner['dispatcher_pid'], owner['dispatcher_identity']))


def test_dispatcher_restart_keeps_order_and_does_not_repeat_active_work(queued, tmp_path):
    bridge, instance = queued
    first = bridge.queue_add(instance, 'hold:release')
    turn = running(bridge, first)
    second = bridge.queue_add(instance, 'second')
    third = bridge.queue_add(instance, 'third')
    with bridge.store.connect() as db:
        owner = dict(db.execute('SELECT * FROM conversation_queues WHERE session_id=?', (instance,)).fetchone())
    os.kill(owner['dispatcher_pid'], signal.SIGKILL)
    until(lambda: not bridge.queue_list(instance)['dispatcher_running'])
    other = Bridge(bridge.root)
    assert other.run(turn).status == 'running'
    assert [item['content'] for item in other.queue_list(instance)['items']] == ['second', 'third']
    other.queue_move(instance, third['message_id'], 0)
    other.queue_resume(instance)
    (tmp_path / 'release').touch()
    completed(other, second)
    assert [row['prompt'] for row in bridge.runs()] == ['hold:release', 'third', 'second']


def test_private_context_is_rebound_explicitly_after_dispatcher_loss(setup_proxy):
    bridge, instance = setup_proxy()
    bridge.queue_pause(instance)
    package = context_package()
    item = bridge.queue_add(instance, 'inspect-context', context_package=package)
    try:
        shutdown(bridge, instance)
        other = Bridge(bridge.root)
        other.queue_resume(instance)
        until(lambda: other.message_get(item['message_id'])['state'] == 'blocked')
        assert other.queue_list(instance)['reason'] == 'context_required'
        assert not bridge.runs()
        other.queue_resume(instance, message_id=item['message_id'], context_package=package)
        result = completed(other, item)
        assert json.loads(other.run(result['turn_id']).text)['developer']
        saved = repr(QueueStore(bridge.store).get(item['message_id']))
        assert 'selected fixture rule' not in saved
        assert 'The fixture source says blue.' not in saved
    finally:
        request(bridge.root, instance, {'action': 'shutdown'}, start=False)


def test_failed_turn_pauses_tail_without_retrying_it(queued):
    bridge, instance = queued
    bridge.queue_pause(instance)
    first = bridge.queue_add(instance, 'fail')
    second = bridge.queue_add(instance, 'second')
    bridge.queue_resume(instance)
    until(lambda: bridge.message_get(first['message_id'])['state'] == 'failed')
    assert bridge.queue_list(instance)['paused']
    assert bridge.message_get(second['message_id'])['turn_id'] is None
    bridge.recover()
    assert len(bridge.runs()) == 1
    bridge.queue_resume(instance)
    completed(bridge, second)
    assert len(bridge.runs()) == 2
    assert native_history(bridge, instance) == {
        'methods': ['thread/start', 'thread/resume'], 'prompts': ['fail', 'second']}


def test_missing_private_binding_cannot_cancel_active_work(queued):
    bridge, instance = queued
    first = bridge.queue_add(instance, 'hold:never')
    turn = running(bridge, first)
    second = bridge.queue_add(instance, 'private follow-up', context_package=context_package())
    shutdown(bridge, instance)
    with pytest.raises(BridgeError) as error:
        bridge.queue_dispatch(instance, second['message_id'], mode='interrupt', expected_turn_id=turn)
    assert error.value.code == 'context_required'
    assert not bridge.run(turn).snapshot['stop_requested']
    assert bridge.message_get(second['message_id'])['turn_id'] is None


def test_cancel_pending_replacement_keeps_tail_paused(queued):
    bridge, instance = queued
    bridge.queue_pause(instance)
    replacement = bridge.queue_add(instance, 'replacement')
    bridge.queue_add(instance, 'later')
    shutdown(bridge, instance)
    from agentbridge.queueing.delivery import dispatch
    dispatch(bridge.store, instance, replacement['message_id'], 'interrupt')
    removed = bridge.queue_delete(instance, replacement['message_id'])
    assert removed['state'] == 'cancelled'
    assert bridge.queue_list(instance)['paused']
    assert [item['content'] for item in bridge.queue_list(instance)['items']] == ['later']


def test_queue_order_is_checked_again_at_atomic_turn_admission(queued, monkeypatch):
    bridge, instance = queued
    bridge.queue_pause(instance)
    first = bridge.queue_add(instance, 'first')
    second = bridge.queue_add(instance, 'second')
    shutdown(bridge, instance)
    QueueStore(bridge.store).resume(instance)
    original = bridge.store.admit

    def competing_edit(*args, **kwargs):
        bridge.queue_move(instance, second['message_id'], 0)
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge.store, 'admit', competing_edit)
    row = QueueStore(bridge.store).get(first['message_id'])
    with pytest.raises(BridgeError) as error:
        bridge.submit(instance, row['content'], options=RunOptions(**json.loads(row['options'])),
                      message_id=row['id'])
    assert error.value.code == 'busy'
    assert not bridge.runs()
    assert [item['content'] for item in bridge.queue_list(instance)['items']] == ['second', 'first']


def test_admission_receipt_fences_recovery_after_launch_loss(queued, monkeypatch):
    bridge, instance = queued
    bridge.queue_pause(instance)
    first = bridge.queue_add(instance, 'first')
    second = bridge.queue_add(instance, 'second')
    shutdown(bridge, instance)
    QueueStore(bridge.store).resume(instance)

    def lost(*args, **kwargs):
        raise OSError('Synthetic loss after admission')

    monkeypatch.setattr('agentbridge.run_launcher.launch', lost)
    row = QueueStore(bridge.store).get(first['message_id'])
    with pytest.raises(OSError):
        bridge.submit(instance, row['content'], options=RunOptions(**json.loads(row['options'])),
                      message_id=row['id'])
    value = bridge.message_get(first['message_id'])
    assert value['turn_id'] and value['queue_state'] == 'dispatched'
    with bridge.store.connect() as db:
        db.execute('UPDATE runs SET created=0 WHERE id=?', (value['turn_id'],))
    assert bridge.recover()['interrupted'] == [value['turn_id']]
    assert bridge.queue_list(instance)['paused']
    assert bridge.message_get(second['message_id'])['state'] == 'queued'
    assert len(bridge.runs()) == 1


def test_pending_messages_fence_archive_delete_and_route_changes(queued):
    bridge, instance = queued
    bridge.queue_pause(instance)
    item = bridge.queue_add(instance, 'pending')
    for action in (lambda: bridge.instance_archive(instance),
                   lambda: bridge.instance_delete(instance),
                   lambda: bridge.instance_update(instance, model='fixture-model')):
        with pytest.raises(BridgeError) as error:
            action()
        assert error.value.code == 'busy'
    bridge.queue_delete(instance, item['message_id'])
    assert bridge.instance_archive(instance)['state'] == 'archived'
    with pytest.raises(BridgeError) as error:
        bridge.queue_add(instance, 'too late')
    assert error.value.code == 'instance_archived'


def test_legacy_active_transport_rejects_steering_before_queuing(setup_proxy):
    bridge, instance = setup_proxy()
    turn, _ = bridge.store.admit('legacy-run', instance, 'hold', RunOptions(), None)
    try:
        with pytest.raises(BridgeError) as error:
            bridge.message_create(instance, 'live input', delivery='steer')
        assert error.value.code == 'steering_unsupported'
        assert not bridge.queue_list(instance)['items']
    finally:
        bridge.store.finish(turn, 'cancelled')


def test_transcript_keeps_pending_and_steered_message_identities(queued):
    bridge, instance = queued
    first = bridge.queue_add(instance, 'hold:never')
    turn = running(bridge, first)
    pending = bridge.queue_add(instance, 'UNSTARTED_QUEUE_INPUT_SENTINEL')
    addition = bridge.message_create(instance, 'correction', delivery='steer')
    until(lambda: bridge.message_get(addition['message_id'])['state'] == 'delivered')
    messages = bridge.messages(instance, role='user')
    assert {item['message_id'] for item in messages} == {
        first['message_id'], pending['message_id'], addition['message_id']}
    assert next(item for item in messages if item['content'] == 'correction')['delivery_state'] == 'delivered'
    assert 'correction' in bridge.export_context(instance).text
    assert 'UNSTARTED_QUEUE_INPUT_SENTINEL' not in bridge.export_context(instance).text
    bridge.queue_delete(instance, pending['message_id'])
    assert pending['message_id'] not in {item['message_id'] for item in bridge.messages(instance)}
    bridge.turn_stop(turn, wait=True)


def test_queue_cli_uses_the_same_persistent_records(queued, capsys):
    bridge, instance = queued
    base = ['--root', str(bridge.root), 'queues']
    cli.main([*base, 'pause', instance])
    capsys.readouterr()
    cli.main([*base, 'add', instance, 'from cli', '--idempotency-key', 'cli'])
    item = json.loads(capsys.readouterr().out)
    cli.main([*base, 'list', instance])
    assert json.loads(capsys.readouterr().out)['items'][0]['message_id'] == item['message_id']
    cli.main([*base, 'move', instance, item['message_id'], '--position', '0'])
    assert json.loads(capsys.readouterr().out)['total'] == 1
    cli.main([*base, 'delete', instance, item['message_id']])
    assert json.loads(capsys.readouterr().out)['state'] == 'cancelled'
    cli.main([*base, 'resume', instance])
    assert json.loads(capsys.readouterr().out)['total'] == 0


def test_queue_executes_after_submitting_cli_processes_exit(queued):
    bridge, instance = queued
    from agentbridge.security import base_environment
    environment = base_environment()
    environment.update({key: os.environ[key] for key in ('FIXTURE_PROXY_KEY', 'FIXTURE_MANAGEMENT_KEY')})
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    command = [sys.executable, '-m', 'agentbridge', '--root', str(bridge.root), 'queues']
    bridge.queue_pause(instance)
    admission = subprocess.run([*command, 'add', instance, 'after client exit'],
                               env=environment, capture_output=True, text=True, check=True, timeout=15)
    item = json.loads(admission.stdout)
    subprocess.run([*command, 'resume', instance], env=environment,
                   capture_output=True, check=True, timeout=15)
    completed(bridge, item)
    assert len(bridge.runs()) == 1


def test_v9_store_migration_is_safe_for_concurrent_clients(queued):
    bridge, instance = queued
    with bridge.store.connect() as db:
        latest = db.execute('SELECT version FROM metadata').fetchone()[0]
        db.execute('DROP TABLE conversation_queues')
        db.execute('DROP TABLE queued_messages')
        db.execute('ALTER TABLE session_routing DROP COLUMN affinity_account_id')
        db.execute('UPDATE metadata SET version=9')
    with ThreadPoolExecutor(4) as executor:
        stores = list(executor.map(lambda _: Store(bridge.root), range(4)))
    upgraded = stores[0]
    with upgraded.connect() as db:
        assert db.execute('SELECT version FROM metadata').fetchone()[0] == latest
    assert QueueStore(upgraded).snapshot(instance)['total'] == 0
    assert QueueStore(Store(bridge.root)).snapshot(instance)['version'] == 1
