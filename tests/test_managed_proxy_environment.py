"""The detached proxy keeps only the worker's reviewed outbound route."""

from agentbridge.proxy import managed
from agentbridge.security import base_environment


def test_run_worker_environment_keeps_the_host_proxy_route(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:18080')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('HOST_SECRET_FIXTURE', 'never-inherit')
    environment = base_environment()
    assert environment['HTTPS_PROXY'] == 'http://127.0.0.1:18080'
    assert environment['NO_PROXY'] == '127.0.0.1,localhost'
    assert 'HOST_SECRET_FIXTURE' not in environment


def test_managed_supervisor_inherits_worker_proxy_without_host_secrets(tmp_path, monkeypatch):
    captured = {}

    class ConnectedSocket:
        def __init__(self, *_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def settimeout(self, _value):
            pass

        def connect(self, _address):
            pass

    def start(_argv, **options):
        captured.update(options['env'])

    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:18080')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('HOST_SECRET_FIXTURE', 'never-inherit')
    monkeypatch.setattr(managed.subprocess, 'Popen', start)
    monkeypatch.setattr(managed.socket, 'socket', ConnectedSocket)
    managed.ManagedProxyClient(tmp_path)._start_supervisor()
    assert captured['HTTPS_PROXY'] == 'http://127.0.0.1:18080'
    assert captured['NO_PROXY'] == '127.0.0.1,localhost'
    assert 'HOST_SECRET_FIXTURE' not in captured
