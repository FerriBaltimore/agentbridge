"""Private native layouts are validated conservatively; fixtures own all files."""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from agentbridge import Account, Bridge
from agentbridge.errors import BridgeError
from agentbridge import native
from fixtures.test_proxy_account_fixture import (register_verified_proxy_account,
                                                seed_authenticated_proxy_account)
from test_proxy_management import EMPTY_CONFIG, local_management


def artifact(home, version='0.153.0', native_id='fixture-native', engine='codex'):
    path = (home / 'sessions' / f'rollout-fixture-{native_id}.jsonl' if engine == 'codex'
            else home / 'projects' / 'fixture' / f'{native_id}.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    record = ({'type': 'session_meta', 'payload': {'id': native_id, 'cli_version': version}}
              if engine == 'codex' else {'type': 'user', 'version': version, 'sessionId': native_id})
    path.write_text(json.dumps(record) + '\n')
    return path


def cli(monkeypatch, version):
    monkeypatch.setattr(native.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout='codex-cli ' + version))


@pytest.mark.parametrize('directory', [True, False])
def test_links_inside_same_home_are_rejected_before_resolving(tmp_path, directory):
    actual = tmp_path / 'actual'
    if directory:
        actual.mkdir()
        (actual / 'file').write_text('fixture')
    else:
        actual.write_text('fixture')
    (tmp_path / 'alias').symlink_to(actual, target_is_directory=directory)
    with pytest.raises(BridgeError) as caught:
        native.safe_path(tmp_path, 'alias/file' if directory else 'alias')
    assert caught.value.code == 'unsafe_path'


@pytest.mark.parametrize('engine', ['codex', 'claude'])
def test_native_copy_accepts_observed_equal_version_only(tmp_path, monkeypatch, engine):
    source = artifact(tmp_path / 'source', engine=engine)
    cli(monkeypatch, '0.153.0')
    assert native.compatible(engine, source, ['fake'], 'fixture-native')
    for version in ['0.154.0', '0.153.1', '1.0.0', '0.152.0', '0.153.0-preview']:
        cli(monkeypatch, version)
        assert not native.compatible(engine, source, ['fake'], 'fixture-native')


@pytest.mark.parametrize('record', [[], None, 5, {'payload': []},
    {'type': 'future_meta', 'payload': {'cli_version': '0.153.0'}},
    {'type': 'session_meta', 'payload': {'id': 'fixture-native'}},
    {'type': 'session_meta', 'payload': {'id': 'another-session', 'cli_version': '0.153.0'}}])
def test_native_metadata_missing_drifted_or_wrong_identity_never_claims_compatibility(
        tmp_path, monkeypatch, record):
    source = artifact(tmp_path)
    source.write_text(json.dumps(record) + '\n')
    cli(monkeypatch, '0.153.0')
    assert not native.compatible('codex', source, ['fake'], 'fixture-native')


def test_bounded_metadata_scan_does_not_parse_an_unbounded_line(tmp_path, monkeypatch):
    source = artifact(tmp_path)
    source.write_text('x' * (1024 * 1024 + 1))
    monkeypatch.setattr(native.subprocess, 'run', lambda *args, **kwargs: pytest.fail('No version probe needed'))
    assert not native.compatible('codex', source, ['fake'], 'fixture-native')


@pytest.mark.parametrize('value', [None, 153, '0.153.0-preview', 'v0.153.0', '0.153.0+build',
                                  'codex 0.153.0, linked to 0.152.0'])
def test_ambiguous_or_nonrelease_version_is_not_promoted_to_a_verified_release(value):
    assert native.version_tuple(value) is None


def database(path, marker, *, optional=False, drift=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        if drift:
            db.execute('CREATE TABLE future_threads(id TEXT PRIMARY KEY)')
            return
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, marker TEXT'
                   + (', future_metadata TEXT' if optional else '') + ')')
        db.execute('INSERT INTO threads(id,rollout_path,marker) VALUES(?,?,?)',
                   ('fixture-native', '/fixture/old-path', marker))
        if optional:
            db.execute("UPDATE threads SET future_metadata='retain-destination-metadata'")


def test_native_indexes_use_numeric_generation_and_preserve_unrelated_columns(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    database(source / 'state_9.sqlite', 'obsolete-source')
    database(source / 'state_10.sqlite', 'current-source')
    database(target / 'state_9.sqlite', 'obsolete-target')
    database(target / 'state_10.sqlite', 'current-target', optional=True)
    destination = target / 'sessions' / 'fixture.jsonl'
    native.index_copy(source, target, 'fixture-native', destination)
    with sqlite3.connect(target / 'state_10.sqlite') as db:
        row = db.execute('SELECT rollout_path,marker,future_metadata FROM threads').fetchone()
    assert row == (str(destination), 'current-source', 'retain-destination-metadata')
    with sqlite3.connect(target / 'state_9.sqlite') as db:
        assert db.execute('SELECT marker FROM threads').fetchone()[0] == 'obsolete-target'


@pytest.mark.parametrize('side', ['source', 'target'])
def test_newest_unknown_index_schema_does_not_fall_back_to_an_old_database(tmp_path, side):
    source, target = tmp_path / 'source', tmp_path / 'target'
    database(source / 'state_9.sqlite', 'old-source')
    database(target / 'state_9.sqlite', 'old-target')
    database((source if side == 'source' else target) / 'state_10.sqlite', 'unknown', drift=True)
    with pytest.raises(BridgeError) as caught:
        native.index_copy(source, target, 'fixture-native', target / 'fixture.jsonl')
    assert caught.value.code == 'native_index_unverified'
    with sqlite3.connect(target / 'state_9.sqlite') as db:
        assert db.execute('SELECT marker FROM threads').fetchone()[0] == 'old-target'


def test_native_transfer_validation_is_unavailable_for_proxy_accounts(tmp_path):
    bridge = Bridge(tmp_path.parent / f'{tmp_path.name}-store')
    register_verified_proxy_account(bridge.store, 'source', 19501)
    register_verified_proxy_account(bridge.store, 'target', 19502)
    bridge.store.add_session('source-session', 'source', str(tmp_path), 'fixture-model')
    session = bridge.get_session('source-session')
    with bridge.store.connect() as db:
        db.execute('UPDATE sessions SET native_id=? WHERE id=?', ('native-fixture', session['id']))
    result = bridge.transfer(session['id'], 'target', mode='native', validate_only=True)
    assert result['supported'] is False and result['reason'] == 'native_continuation_unavailable'


def test_portable_transfer_validation_requires_observed_target(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path.parent / f'{tmp_path.name}-store')
    register_verified_proxy_account(bridge.store, 'source', 19504)
    register_verified_proxy_account(bridge.store, 'target', 19505)
    bridge.store.add_session('source-session', 'source', str(tmp_path), 'fixture-model')
    monkeypatch.setattr(bridge.routes, 'observation', lambda *_args, **_kwargs: None)
    result = bridge.transfer('source-session', 'target', mode='portable', validate_only=True)
    assert result == {'supported': False, 'mode': 'portable', 'same_engine': True,
                      'reason': 'proxy_binding_unverified'}


def test_auto_transfer_uses_portable_evidence_between_proxy_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_PROXY_KEY', 'fixture-client-key')
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', 'fixture-management-key')
    responses = {
        '/v0/management/config': (200, EMPTY_CONFIG, {}),
        '/v0/management/auth-files': (200, {'files': [{
            'name': 'target.json', 'auth_index': 'target-index', 'account_type': 'oauth',
            'provider': 'codex', 'status': 'active', 'disabled': False,
            'unavailable': False, 'source': 'file', 'runtime_only': False,
            'id_token': {'chatgpt_account_id': 'target-person'}, 'cooldowns': []}]}, {}),
        '/v0/management/auth-files/models?name=target.json': (
            200, {'models': [{'id': 'fixture-model'}]}, {}),
    }
    with local_management(responses) as (port, _):
        bridge = Bridge(tmp_path.parent / f'{tmp_path.name}-store')
        register_verified_proxy_account(bridge.store, 'source', 19503)
        seed_authenticated_proxy_account(bridge.store, Account('target', 'codex', provider='codex',
            supported_models=('fixture-model',), proxy_base_url=f'http://127.0.0.1:{port}/v1',
            key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY'),
            observe_local=True)
        assert bridge.routes.observe(bridge.account('target')) is not None
        bridge.store.add_session('source-session', 'source', str(tmp_path), 'fixture-model')
        session = bridge.get_session('source-session')
        result = bridge.transfer(session['id'], 'target', mode='auto',
                                 idempotency_key='transfer-1')
        assert result['transfer_mode'] == 'portable'
        assert result['fallback_reason'] is None
        assert result['context_omissions'] == 0
        monkeypatch.setattr(bridge.routes, 'observation', lambda *_args, **_kwargs: None)
        replay = bridge.transfer(session['id'], 'target', mode='auto',
                                 idempotency_key='transfer-1')
        assert replay['id'] == result['id']
        assert replay['replayed'] is True


def test_verified_native_copy_preserves_file_and_excludes_account_configuration(tmp_path, monkeypatch):
    source_home, target_home = tmp_path / 'source', tmp_path / 'target'
    source = artifact(source_home)
    (source_home / 'auth.json').write_text('fake fixture credential, never copy')
    cli(monkeypatch, '0.153.0')
    result = native.copy_session('codex', 'fixture-native', source_home, target_home, ['fake'])
    target = target_home / source.relative_to(source_home)
    assert result == 'fixture-native' and target.read_bytes() == source.read_bytes()
    assert not (target_home / 'auth.json').exists()
    assert native.copy_session('codex', 'fixture-native', source_home, target_home, ['fake']) == result
    target.write_text('diverged destination')
    with pytest.raises(BridgeError) as caught:
        native.copy_session('codex', 'fixture-native', source_home, target_home, ['fake'])
    assert caught.value.code == 'native_session_diverged'
    assert target.read_text() == 'diverged destination'
