"""Offline inspection fixtures never resolve accounts, credentials or agents."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentbridge import BridgeError
from agentbridge import provider_fingerprint as fingerprint


def schemas(directory, *, method='model/list'):
    root = Path(directory)
    root.mkdir()
    (root / 'ClientRequest.json').write_text(json.dumps({'oneOf': [
        {'properties': {'method': {'enum': [method]}, 'params': {'type': 'object'}}}]}))


@pytest.fixture
def native(monkeypatch):
    calls = []
    monkeypatch.setattr(fingerprint.shutil, 'which', lambda engine: '/fixture/' + engine)

    def run(command, *, env, cwd, timeout, max_bytes):
        calls.append((command, env, cwd, timeout, max_bytes))
        assert env['HOME'] == cwd and Path(cwd).is_dir()
        assert env['CLAUDE_CONFIG_DIR'].startswith(cwd)
        assert env['CODEX_HOME'].startswith(cwd)
        assert env['TMPDIR'] == cwd
        if command[-1] == '--version':
            return b'codex-cli 0.153.0\n' if command[0].endswith('codex') else b'2.1.266 (Claude Code)\n'
        if command[-1] == '--help':
            return b'Usage: claude\n  --allowedTools <tools>\n  -p, --print\n  --future-mode <mode>\n'
        assert command[1:4] == ['app-server', 'generate-json-schema', '--out']
        schemas(command[4])
        return b''

    monkeypatch.setattr(fingerprint, 'read_output', run)
    return calls


def test_canonical_hash_ignores_mapping_order_but_preserves_arrays_null_and_false():
    assert fingerprint.canonical_hash({'b': [1, 2], 'a': None}) == fingerprint.canonical_hash({'a': None, 'b': [1, 2]})
    values = [None, False, 0, [], [1, 2], [2, 1]]
    assert len({fingerprint.canonical_hash(value) for value in values}) == len(values)


@pytest.mark.parametrize('value', [{1: 'coerced'}, float('nan'), float('inf'), (1, 2), {'bad': object()}, '\ud800'])
def test_non_json_or_ambiguous_hash_inputs_fail(value):
    with pytest.raises(BridgeError):
        fingerprint.canonical_hash(value)


def test_canonical_hash_rejects_excess_depth_and_bytes(monkeypatch):
    value = None
    for _ in range(102):
        value = [value]
    with pytest.raises(BridgeError):
        fingerprint.canonical_hash(value)
    monkeypatch.setattr(fingerprint, 'MAX_TOTAL_BYTES', 10)
    with pytest.raises(BridgeError):
        fingerprint.canonical_hash('a' * 10)


@pytest.mark.parametrize('data', [b'{"a":1,"a":2}', b'{"value":NaN}', b'[]trailing', b'\xff'])
def test_schema_json_rejects_duplicates_nonfinite_and_invalid_encoding(data):
    with pytest.raises(BridgeError):
        fingerprint._json(data)


def test_codex_inspection_is_bounded_clean_and_reproducible(native, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-secret')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-secret')
    monkeypatch.setenv('CURSOR_API_KEY', 'fixture-secret')
    first = fingerprint.inspect_surface('codex')
    second = fingerprint.inspect_surface('codex')
    assert first == second
    assert first['component'] == 'codex-cli' and first['version'] == '0.153.0'
    assert first['evidence_kind'] == 'codex_json_schema_default_v1'
    assert first['surface_names'] == ['model/list']
    assert len(first['structural_hash']) == 64
    for command, env, cwd, timeout, maximum in native:
        assert 'fixture-secret' not in env.values()
        assert not Path(cwd).exists()
        assert timeout <= 20 and maximum <= 16384
        assert '--experimental' not in command
        assert all(word not in command for word in ('login', 'exec', 'resume'))


def test_claude_help_is_observation_only_and_keeps_camelcase_flags(native):
    value = fingerprint.inspect_surface('claude')
    assert value['version'] == '2.1.266'
    assert value['surface_names'] == ['--allowedTools', '--future-mode', '--print']
    assert value['evidence_kind'] == 'cli_help_observation_v1'
    assert 'cannot_certify_structural_equivalence' in value['limitations']
    assert [call[0][1:] for call in native] == [['--version'], ['--help'], ['--version']]


@pytest.mark.parametrize('engine', ['codex', 'claude'])
def test_native_version_change_during_inspection_is_decisive_drift(native, monkeypatch, engine):
    original = fingerprint.read_output
    count = 0
    def updated(command, **kwargs):
        nonlocal count
        if command[-1] == '--version':
            count += 1
            if count == 2:
                return b'codex-cli 0.154.0\n' if engine == 'codex' else b'2.1.267 (Claude Code)\n'
        return original(command, **kwargs)
    monkeypatch.setattr(fingerprint, 'read_output', updated)
    with pytest.raises(BridgeError) as error:
        fingerprint.inspect_surface(engine)
    assert error.value.code == 'provider_contract_changed'
    assert error.value.details == {'engine': engine, 'component': engine + '-cli',
                                  'version': '0.153.0' if engine == 'codex' else '2.1.266'}


@pytest.mark.parametrize('version', [b'codex-cli 0.153.0-preview', b'codex-cli 0.153.0+custom', b'anything 0.153.0', b'\xff'])
def test_unknown_version_does_not_bind_to_stable(native, monkeypatch, version):
    original = fingerprint.read_output
    monkeypatch.setattr(fingerprint, 'read_output', lambda command, **kwargs:
                        version if command[-1] == '--version' else original(command, **kwargs))
    value = fingerprint.inspect_surface('codex')
    assert value['version'] is None and 'unrecognized_version_output' in value['limitations']
    assert version.decode('utf-8', errors='replace') not in json.dumps(value)


def test_failed_process_has_safe_error_and_temp_cleanup(native, monkeypatch):
    homes = []
    def fail(command, **kwargs):
        homes.append(kwargs['cwd'])
        raise BridgeError('provider_timeout', 'fixture-secret')
    monkeypatch.setattr(fingerprint, 'read_output', fail)
    with pytest.raises(BridgeError) as error:
        fingerprint.inspect_surface('codex')
    assert error.value.code == 'provider_timeout' and 'fixture-secret' not in str(error.value)
    assert homes and all(not Path(path).exists() for path in homes)


@pytest.mark.parametrize('problem', ['symlink', 'too_large', 'too_many', 'duplicate', 'not_object', 'unknown_file', 'deep'])
def test_generated_schema_drift_and_bounds_fail(tmp_path, monkeypatch, problem):
    root = tmp_path / 'schemas'
    schemas(root)
    if problem == 'symlink':
        (root / 'link.json').symlink_to(root / 'ClientRequest.json')
    elif problem == 'too_large':
        monkeypatch.setattr(fingerprint, 'MAX_FILE_BYTES', 1)
    elif problem == 'too_many':
        monkeypatch.setattr(fingerprint, 'MAX_FILES', 0)
    elif problem == 'duplicate':
        (root / 'bad.json').write_text('{"type":"object","type":"array"}')
    elif problem == 'not_object':
        (root / 'bad.json').write_text('[]')
    elif problem == 'unknown_file':
        (root / 'bad.txt').write_text('fixture')
    else:
        (root / 'a' / 'b' / 'c' / 'd' / 'e').mkdir(parents=True)
    with pytest.raises(BridgeError):
        fingerprint._schemas(root)


def test_method_changes_and_unknown_method_shapes_are_detected(tmp_path):
    first, second = tmp_path / 'a', tmp_path / 'b'
    schemas(first)
    schemas(second, method='future/method')
    assert fingerprint.canonical_hash(fingerprint._schemas(first)) != fingerprint.canonical_hash(fingerprint._schemas(second))
    with pytest.raises(BridgeError):
        fingerprint._methods([{'path': 'ClientRequest.json', 'schema': {'oneOf': [{'properties': {'method': {'const': 'new'}}}]}}])


def test_generated_schema_root_cannot_redirect_to_unrelated_data(tmp_path):
    other = tmp_path / 'unrelated'
    schemas(other)
    root = tmp_path / 'schemas'
    root.symlink_to(other, target_is_directory=True)
    with pytest.raises(BridgeError):
        fingerprint._schemas(root)


def cursor_package(root, *, version='1.0.31', status='finished'):
    types = '''from dataclasses import dataclass
from typing import Literal
RunResultStatus = Literal[%r, "error", "cancelled", "expired"]
''' % status
    for name in ('AgentOptions', 'LocalAgentOptions', 'SendOptions', 'SDKModel', 'RunResult'):
        types += '@dataclass\nclass ' + name + ':\n    value: str | None = None\n'
    root.mkdir()
    (root / 'types.py').write_text(types)
    bridge = root / '_vendor' / 'bridge'
    (bridge / 'dist').mkdir(parents=True)
    (bridge / 'dist' / 'constants.js').write_text('''export const CURSOR_SDK_BRIDGE_PROTOCOL_VERSION = "sdk.v1";
export const CURSOR_SDK_BRIDGE_CAPABILITIES = ["agent.create", "run.wait",];
''')
    package = bridge / 'node_modules' / '@cursor' / 'sdk'
    package.mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'name': '@cursor/sdk', 'version': version}))
    return root


@pytest.fixture
def cursor(tmp_path, monkeypatch):
    root = cursor_package(tmp_path / 'cursor_sdk')
    distribution = SimpleNamespace(version='1.0.31', locate_file=lambda name: root)
    monkeypatch.setattr(fingerprint.importlib.metadata, 'distribution', lambda name: distribution)
    monkeypatch.setattr(fingerprint, 'read_output', lambda *args, **kwargs: pytest.fail('No provider subprocess for SDK inspection'))
    return root, distribution


def test_cursor_reads_same_interpreter_artifacts_without_importing_sdk(cursor):
    value = fingerprint.inspect_surface('cursor')
    assert value['component'] == 'cursor-sdk' and value['version'] == '1.0.31'
    assert value['evidence_kind'] == 'cursor_python_ast_bridge_v1'
    assert 'run.wait' in value['surface_names'] and 'python:RunResultStatus' in value['surface_names']
    assert 'bundled_sdk_version_mismatch' not in value['limitations']


def test_cursor_ast_ignores_comments_but_detects_type_and_protocol_changes(cursor):
    root, _ = cursor
    original = fingerprint.inspect_surface('cursor')['structural_hash']
    types = root / 'types.py'
    types.write_text('# New comment\n' + types.read_text() + '\n')
    assert fingerprint.inspect_surface('cursor')['structural_hash'] == original
    types.write_text(types.read_text().replace('finished', 'future_terminal'))
    changed = fingerprint.inspect_surface('cursor')['structural_hash']
    assert changed != original
    bridge = root / '_vendor/bridge/dist/constants.js'
    bridge.write_text(bridge.read_text().replace('sdk.v1', 'sdk.v2'))
    assert fingerprint.inspect_surface('cursor')['structural_hash'] != changed


def test_cursor_same_structure_new_version_requires_explicit_binding(cursor):
    root, distribution = cursor
    first = fingerprint.inspect_surface('cursor')
    distribution.version = '1.0.32'
    package = root / '_vendor/bridge/node_modules/@cursor/sdk/package.json'
    package.write_text(package.read_text().replace('1.0.31', '1.0.32'))
    second = fingerprint.inspect_surface('cursor')
    assert first['structural_hash'] == second['structural_hash']
    assert second['version'] == '1.0.32'
    assert 'exact_binding_review_required' in second['limitations']


def test_cursor_mismatched_bundled_version_cannot_be_reused(cursor):
    _, distribution = cursor
    distribution.version = '1.0.32'
    with pytest.raises(BridgeError) as error:
        fingerprint.inspect_surface('cursor')
    assert error.value.code == 'provider_contract_changed'
    assert error.value.details == {'engine': 'cursor', 'version': '1.0.32', 'component': 'cursor-sdk'}


def test_cursor_malformed_sources_are_rejected_without_executing_them(cursor):
    root, _ = cursor
    (root / 'types.py').write_text('raise RuntimeError("fixture-secret")\n')
    with pytest.raises(BridgeError) as error:
        fingerprint.inspect_surface('cursor')
    assert 'fixture-secret' not in str(error.value)


def test_cursor_method_binding_changes_are_structural_drift(cursor):
    root, _ = cursor
    source = root / 'types.py'
    source.write_text(source.read_text() + '''
class FixtureResult:
    @staticmethod
    def decode(value: str) -> str:
        return value
''')
    first = fingerprint.inspect_surface('cursor')['structural_hash']
    source.write_text(source.read_text().replace('@staticmethod', '@classmethod'))
    assert fingerprint.inspect_surface('cursor')['structural_hash'] != first


def test_unknown_engine_missing_executable_and_missing_sdk(monkeypatch):
    with pytest.raises(BridgeError) as error:
        fingerprint.inspect_surface('unrecognized')
    assert error.value.code == 'invalid_engine'
    monkeypatch.setattr(fingerprint.shutil, 'which', lambda _: None)
    with pytest.raises(BridgeError) as error:
        fingerprint.inspect_surface('codex')
    assert error.value.code == 'provider_unavailable'
    def missing(_):
        raise fingerprint.importlib.metadata.PackageNotFoundError()
    monkeypatch.setattr(fingerprint.importlib.metadata, 'distribution', missing)
    with pytest.raises(BridgeError) as error:
        fingerprint.inspect_surface('cursor')
    assert error.value.code == 'provider_unavailable'
