"""Bridge launch sites use the same managed Codex release and state root."""

from pathlib import Path

import pytest

from agentbridge import Account, RunOptions
from agentbridge.account_probe import CodexAppServerProbe
from agentbridge.codex_executable import codex_argv
from agentbridge.error_observer import provider_version
from agentbridge.errors import BridgeError
from agentbridge.transports import command


def _account(tmp_path, *, custom=()):
    return Account('fixture', 'codex', home=str(tmp_path / 'home'),
                   provider='codex', supported_models=('fixture-model',),
                   proxy_base_url='http://127.0.0.1:8317/v1',
                   key_env='CLIENT_KEY', command=custom)


def test_default_turn_and_probe_use_one_managed_codex(tmp_path, monkeypatch):
    from agentbridge.bundle import runtime

    state_root = tmp_path / 'state'
    executable = tmp_path / 'managed' / 'bin' / 'codex'
    calls = []

    def resolve(component, root):
        calls.append((component, Path(root)))
        return executable

    monkeypatch.setattr(runtime, 'resolve_binary', resolve)
    account = _account(tmp_path)
    session = {'native_id': None, 'model': 'fixture-model'}
    direct = command(account, session, RunOptions(), state_root=state_root)
    duplex = command(account, session, RunOptions(permission_mode='default'),
                     native_transport=True, state_root=state_root)
    probe = CodexAppServerProbe(account, state_root=state_root)

    assert direct[:2] == [str(executable), 'exec']
    assert duplex[:3] == [str(executable), 'app-server', '--stdio']
    assert probe._command()[:3] == duplex[:3]
    assert calls == [('codex', state_root)] * 3


def test_custom_launcher_is_preserved_without_managed_resolution(tmp_path, monkeypatch):
    from agentbridge.bundle import runtime

    monkeypatch.setattr(runtime, 'resolve_binary',
                        lambda *args: (_ for _ in ()).throw(AssertionError('unexpected')))
    account = _account(tmp_path, custom=('/fixture/custom-codex', '--private-option'))
    assert codex_argv(account, tmp_path / 'state') == ['/fixture/custom-codex', '--private-option']
    assert command(account, {'model': 'fixture-model'}, RunOptions(),
                   state_root=tmp_path / 'state')[:3] == [
                       '/fixture/custom-codex', '--private-option', 'exec']
    assert provider_version(account, tmp_path / 'state') is None


def test_contract_version_queries_the_managed_executable(tmp_path, monkeypatch):
    from agentbridge.bundle import runtime
    from agentbridge import error_observer

    state_root = tmp_path / 'state'
    executable = tmp_path / 'managed' / 'bin' / 'codex'
    monkeypatch.setattr(runtime, 'resolve_binary', lambda component, root: executable)
    observed = []

    def read_output(argv, *, env, timeout, max_bytes):
        observed.append(argv)
        return b'codex-cli 0.153.0\n'

    monkeypatch.setattr(error_observer, 'read_output', read_output)
    assert provider_version(_account(tmp_path), state_root) == '0.153.0'
    assert observed == [[str(executable), '--version']]


def test_missing_managed_codex_fails_before_version_query(tmp_path, monkeypatch):
    from agentbridge.bundle import runtime
    from agentbridge import error_observer

    def unavailable(component, root):
        raise BridgeError('bundled_runtime_unavailable', 'The reviewed runtime is unavailable.')

    monkeypatch.setattr(runtime, 'resolve_binary', unavailable)
    monkeypatch.setattr(error_observer, 'read_output',
                        lambda *args, **kwargs: pytest.fail('No native process expected'))
    with pytest.raises(BridgeError) as caught:
        provider_version(_account(tmp_path), tmp_path / 'state')
    assert caught.value.code == 'bundled_runtime_unavailable'
