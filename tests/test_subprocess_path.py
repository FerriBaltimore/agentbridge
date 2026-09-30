"""Host-bundle dependencies survive trusted child launches without ambient paths."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from agentbridge.errors import BridgeError
from agentbridge import subprocess_path


def bundle(tmp_path, monkeypatch):
    root = tmp_path / 'bundle'
    source = root / 'src' / 'agentbridge'
    source.mkdir(parents=True)
    dependencies = root / 'python'
    dependencies.mkdir()
    (dependencies / 'RUNTIME.json').write_text('{}')
    (dependencies / 'driver.py').write_text('value = 42\n')
    inventory = {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
                 for path in dependencies.iterdir()}
    (root / 'SNAPSHOT-MANIFEST.json').write_text(json.dumps(inventory))
    monkeypatch.setattr(subprocess_path, '__file__', str(source / 'subprocess_path.py'))
    monkeypatch.setenv('PYTHONPATH', '/untrusted/ambient')
    return root


def test_verified_dependency_path_survives_real_subprocess(tmp_path, monkeypatch):
    root = bundle(tmp_path, monkeypatch)
    environment = {'PATH': os.defpath, 'PYTHONPATH': subprocess_path.python_path(),
                   'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run(['/usr/bin/python3', '-P', '-c',
                             'import driver; print(driver.value)'], env=environment,
                            capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout == '42\n'
    assert environment['PYTHONPATH'] == os.pathsep.join(
        (str(root / 'src'), str(root / 'python')))
    (root / 'python' / 'driver.py').write_text('value = 99\n')
    with pytest.raises(BridgeError, match='could not be verified'):
        subprocess_path.python_path()


def test_unlisted_or_symbolic_dependencies_fail_closed(tmp_path, monkeypatch):
    root = bundle(tmp_path, monkeypatch)
    extra = root / 'python' / 'extra.py'
    extra.write_text('raise RuntimeError()')
    with pytest.raises(BridgeError):
        subprocess_path.python_path()
    extra.unlink()
    extra.symlink_to(root / 'python' / 'driver.py')
    with pytest.raises(BridgeError):
        subprocess_path.python_path()


def test_installed_package_ignores_unmanifested_sibling_and_ambient_path(tmp_path, monkeypatch):
    source = tmp_path / 'site-packages' / 'agentbridge'
    source.mkdir(parents=True)
    (tmp_path / 'python').mkdir()
    monkeypatch.setattr(subprocess_path, '__file__', str(source / 'subprocess_path.py'))
    monkeypatch.setenv('PYTHONPATH', '/untrusted/ambient')
    assert subprocess_path.python_path() == str(source.parent)


@pytest.mark.skipif(not os.environ.get('AGENTBRIDGE_TEST_DEPENDENCY_BUNDLE'),
                    reason='An explicit verified host dependency bundle is required.')
def test_bundle_supervisor_opens_postgres_without_installed_driver(managed, tmp_path):
    # Reuse the real PostgreSQL fixture plugin and loopback-only synthetic proxy.
    client, state = managed
    assert (state / 'store-backend.json').exists()
    bundle_root = Path(os.environ['AGENTBRIDGE_TEST_DEPENDENCY_BUNDLE'])
    root = tmp_path / 'host-bundle'
    source = root / 'src' / 'agentbridge'
    shutil.copytree(Path(subprocess_path.__file__).parent, source,
                    ignore=shutil.ignore_patterns('__pycache__', 'assets'))
    shutil.copytree(bundle_root / 'python', root / 'python',
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(bundle_root / 'SNAPSHOT-MANIFEST.json', root / 'SNAPSHOT-MANIFEST.json')
    # /usr/bin/python3 has no psycopg installed: only this bundle can supply it.
    clean = subprocess.run(['/usr/bin/python3', '-I', '-c',
                            'import importlib.util; assert '
                            'importlib.util.find_spec("psycopg") is None'], timeout=10)
    assert clean.returncode == 0
    code = ('from agentbridge.proxy.managed import ManagedProxyClient; '
            'import sys; c=ManagedProxyClient(sys.argv[1]); '
            'r=c.provision("synthetic-pg"); '
            'assert c.is_managed(r,"synthetic-pg"); c.shutdown(); print("verified")')
    environment = {'PATH': os.defpath, 'HOME': str(tmp_path), 'PYTHONPATH': str(root / 'src'),
                   'PYTHONDONTWRITEBYTECODE': '1',
                   'AGENTBRIDGE_CLIPROXY_BIN': os.environ['AGENTBRIDGE_CLIPROXY_BIN']}
    result = subprocess.run(['/usr/bin/python3', '-P', '-c', code, str(state)],
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'verified\n'


# This fixture owns only its synthetic HTTP process and shuts down its supervisor.
from test_managed_proxy import managed  # noqa: E402,F401
