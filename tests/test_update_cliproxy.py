"""Offline release-refresh tests; no real accounts or network requests."""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import tarfile

import pytest

from tools.update_cliproxy import ARCHIVES, RELEASE_ROOT, UpdateError, update_release


def archive(machine, *, unsafe=False):
    body = BytesIO()
    with tarfile.open(fileobj=body, mode='w:gz') as output:
        for name, content in (
                ('cli-proxy-api', b'\x7fELF\x02\x01' + b'\x00' * 12
                 + machine.to_bytes(2, 'little') + b'\x00' * 12),
                ('LICENSE', b'MIT License\nFixture notice.\n')):
            member = tarfile.TarInfo('../escape' if unsafe and name == 'LICENSE' else name)
            member.size = len(content)
            output.addfile(member, BytesIO(content))
    return body.getvalue()


def release(version='7.3.17', *, wrong_hash=False, unsafe=False,
            wrong_arch=False, duplicate_checksum=False):
    base = f'{RELEASE_ROOT}/v{version}'
    responses = {}
    checksums = []
    for platform, (arch, machine) in ARCHIVES.items():
        name = f'CLIProxyAPI_{version}_linux_{arch}_no-plugin.tar.gz'
        content = archive(62 if wrong_arch and platform == 'linux_aarch64' else machine,
                          unsafe=unsafe and platform == 'linux_aarch64')
        responses[f'{base}/{name}'] = content
        digest = sha256(content).hexdigest()
        if wrong_hash and platform == 'linux_aarch64':
            digest = '0' * 64
        checksums.append(f'{digest}  {name}')
    if duplicate_checksum:
        checksums.append(checksums[0])
    responses[f'{base}/checksums.txt'] = ('\n'.join(checksums) + '\n').encode()
    calls = []

    def opener(url, *, timeout):
        calls.append(url)
        assert timeout == 30
        return BytesIO(responses[url])

    return opener, calls


def test_refresh_downloads_both_assets_and_preserves_other_component_pins(tmp_path):
    lock = tmp_path / 'lock.json'
    lock.write_text(json.dumps({'codex': {'version': 'unchanged'}}))
    opener, calls = release()
    result = update_release('v7.3.17', tmp_path / 'download', lock, write=True,
                            opener=opener)
    saved = json.loads(lock.read_text())
    assert saved['codex'] == {'version': 'unchanged'}
    assert saved['cli_proxy_api'] == result
    assert set(result['assets']) == {'linux_x86_64', 'linux_aarch64'}
    assert len(calls) == 3
    for platform, asset in result['assets'].items():
        file = tmp_path / 'download/v7.3.17' / Path(asset['url']).name
        assert file.is_file()
        assert sha256(file.read_bytes()).hexdigest() == asset['sha256']


def test_dry_run_leaves_runtime_lock_unchanged_and_reuses_verified_downloads(tmp_path):
    lock = tmp_path / 'lock.json'
    lock.write_text('{"codex": {"version": "fixed"}}\n')
    opener, calls = release()
    first = update_release('7.3.17', tmp_path / 'download', lock, opener=opener)
    second = update_release('7.3.17', tmp_path / 'download', lock, opener=opener)
    assert first == second
    assert json.loads(lock.read_text()) == {'codex': {'version': 'fixed'}}
    assert len(calls) == 4  # A checksum document each time; archives only once.


@pytest.mark.parametrize('reason', ['wrong_hash', 'unsafe', 'wrong_arch', 'duplicate_checksum'])
def test_rejected_asset_never_updates_lock(tmp_path, reason):
    lock = tmp_path / 'lock.json'
    lock.write_text('{"codex": {"version": "fixed"}}\n')
    opener, _ = release(**{reason: True})
    with pytest.raises(UpdateError):
        update_release('7.3.17', tmp_path / 'download', lock, write=True,
                       opener=opener)
    assert json.loads(lock.read_text()) == {'codex': {'version': 'fixed'}}


def test_rejects_moving_release_aliases(tmp_path):
    with pytest.raises(UpdateError):
        update_release('latest', tmp_path / 'download', tmp_path / 'lock.json')
