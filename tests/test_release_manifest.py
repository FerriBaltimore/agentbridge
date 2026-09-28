"""Offline checks of the release manifest tool and the reproducible sdist rewrite."""

from datetime import datetime, timezone
import gzip
from io import BytesIO
import json
from pathlib import Path
import tarfile

import pytest

from agentbridge import __version__, protocol_versions
from playground.server import API_REVISION
from tools import release_manifest
from tools.reproducible_sdist import normalize_tarball, source_date_epoch

ROOT = Path(__file__).resolve().parents[1]
EPOCH = 1790607492
LOCK = json.loads((ROOT / 'src/agentbridge/bundle/lock.json').read_text())


def fake_dist(tmp_path, version=__version__, arch='x86_64'):
    dist = tmp_path / 'dist'
    dist.mkdir()
    wheel = dist / f'ferran_agentbridge-{version}-py3-none-manylinux_2_28_{arch}.whl'
    sdist = dist / f'ferran_agentbridge-{version}.tar.gz'
    wheel.write_bytes(b'wheel fixture bytes')
    sdist.write_bytes(b'sdist fixture bytes')
    return dist, wheel, sdist


def test_manifest_records_digests_protocols_and_a_reproducible_time(tmp_path, monkeypatch):
    dist, wheel, sdist = fake_dist(tmp_path)
    monkeypatch.setenv('SOURCE_DATE_EPOCH', str(EPOCH))
    path, manifest = release_manifest.write_manifest(dist, arch='x86_64')
    assert path == dist / f'agentbridge-{__version__}.manifest.json'
    assert json.loads(path.read_text()) == manifest
    assert (manifest['schema'], manifest['name'], manifest['version']) == (
        1, 'ferran-agentbridge', __version__)
    assert manifest['python_requires'] == '>=3.11'
    assert manifest['platform'] == 'linux_x86_64'
    assert manifest['wheel'] == {'file': wheel.name, 'sha256': release_manifest.digest(wheel),
                                 'bytes': wheel.stat().st_size}
    assert manifest['sdist'] == {'file': sdist.name, 'sha256': release_manifest.digest(sdist),
                                 'bytes': sdist.stat().st_size}
    assert manifest['protocols'] == {'rpc_contract': 'v2', 'http': 2, 'cli': 1, 'login_start': 2}
    assert manifest['grantbridge_min'] == '1.0.0-rc.1'
    assert manifest['bundle'] == {
        'cli_proxy_api': LOCK['cli_proxy_api']['version'], 'codex': LOCK['codex']['version'],
        'node': LOCK['node']['version'], 'grantbridge_commit': LOCK['grantbridge']['commit']}
    expected_time = datetime.fromtimestamp(EPOCH, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    assert (manifest['built_at'], manifest['source_date_epoch']) == (expected_time, EPOCH)
    assert len(manifest['git_commit']) == 40 and isinstance(manifest['working_tree_dirty'], bool)
    assert 'build_v2.py' in manifest['fullbrain_pin_note']
    assert 'PIN_SHA256' in manifest['fullbrain_pin_note']
    assert release_manifest.validate_manifest(path) == manifest


def test_validation_rejects_tampered_artifacts_and_stale_constants(tmp_path, monkeypatch):
    dist, wheel, _ = fake_dist(tmp_path)
    monkeypatch.delenv('SOURCE_DATE_EPOCH', raising=False)
    path, manifest = release_manifest.write_manifest(dist, arch='x86_64')
    assert manifest['source_date_epoch'] is None
    original = wheel.read_bytes()
    wheel.write_bytes(b'a different wheel')
    with pytest.raises(ValueError, match='differs from its recorded size or digest'):
        release_manifest.validate_manifest(path)
    wheel.write_bytes(original)
    stale = dict(manifest, protocols=dict(manifest['protocols'], login_start=1), version='0.0.1')
    path.write_text(json.dumps(stale))
    with pytest.raises(ValueError) as failure:
        release_manifest.validate_manifest(path)
    assert 'version differs from the checkout' in str(failure.value)
    assert 'protocols differs from the checkout' in str(failure.value)
    wheel.unlink()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='missing beside the manifest'):
        release_manifest.validate_manifest(path)


def test_missing_distribution_is_reported_before_writing(tmp_path):
    dist = tmp_path / 'dist'
    dist.mkdir()
    with pytest.raises(FileNotFoundError, match='build it first'):
        release_manifest.write_manifest(dist, arch='x86_64')
    assert list(dist.iterdir()) == []


def test_command_line_writes_then_checks(tmp_path, capsys):
    dist, _, _ = fake_dist(tmp_path)
    assert release_manifest.main(['--dist', str(dist), '--arch', 'x86_64']) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['version'] == __version__ and len(summary['wheel_sha256']) == 64
    assert release_manifest.main(['--dist', str(dist), '--check']) == 0
    assert 'is valid' in capsys.readouterr().out
    (dist / f'agentbridge-{__version__}.manifest.json').write_text('{"schema": 2}')
    assert release_manifest.main(['--dist', str(dist), '--check']) == 1
    assert 'schema is unsupported' in capsys.readouterr().err


def test_protocol_numbers_match_the_code_and_the_package_version():
    assert protocol_versions.HTTP_API_REVISION == API_REVISION
    assert protocol_versions.protocols() == {'rpc_contract': 'v2', 'http': API_REVISION,
                                             'cli': 1, 'login_start': 2}
    assert release_manifest.project_metadata()['version'] == __version__


def tarball(members, *, mtime, uid, gzip_mtime):
    body = BytesIO()
    with tarfile.open(fileobj=body, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name, content, mode in members:
            info = tarfile.TarInfo(name)
            info.mode, info.mtime, info.uid, info.gid = mode, mtime, uid, uid
            info.uname = info.gname = 'someone'
            if name.endswith('/'):
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, BytesIO(content))
    output = BytesIO()
    with gzip.GzipFile(fileobj=output, mode='wb', mtime=gzip_mtime) as stream:
        stream.write(body.getvalue())
    return output.getvalue()


def test_normalized_sdists_are_byte_identical_and_keep_their_contents(tmp_path):
    members = [('pkg-1.0/b.txt', b'bee', 0o664), ('pkg-1.0/a.py', b'print(1)\n', 0o775),
               ('pkg-1.0/', b'', 0o775)]
    first = tmp_path / 'first.tar.gz'
    second = tmp_path / 'second.tar.gz'
    first.write_bytes(tarball(members, mtime=1700000000.25, uid=1000, gzip_mtime=1700000001))
    second.write_bytes(tarball(list(reversed(members)), mtime=1800000000, uid=2000,
                               gzip_mtime=1800000002))
    assert first.read_bytes() != second.read_bytes()
    normalize_tarball(first, EPOCH)
    normalize_tarball(second, EPOCH)
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes()[4:8] == b'\0\0\0\0'
    with tarfile.open(first, 'r:gz') as archive:
        names = archive.getnames()
        assert names == ['pkg-1.0', 'pkg-1.0/a.py', 'pkg-1.0/b.txt']
        for member in archive.getmembers():
            assert (member.mtime, member.uid, member.gid, member.uname, member.gname) == (
                EPOCH, 0, 0, '', '')
            assert member.pax_headers == {}
        assert archive.getmember('pkg-1.0/a.py').mode == 0o755
        assert archive.getmember('pkg-1.0/b.txt').mode == 0o644
        assert archive.getmember('pkg-1.0').isdir()
        assert archive.extractfile('pkg-1.0/a.py').read() == b'print(1)\n'
        assert archive.extractfile('pkg-1.0/b.txt').read() == b'bee'
    assert list(tmp_path.glob('.sdist-normalize-*')) == []


def test_source_date_epoch_is_parsed_strictly(monkeypatch):
    monkeypatch.delenv('SOURCE_DATE_EPOCH', raising=False)
    assert source_date_epoch() is None
    monkeypatch.setenv('SOURCE_DATE_EPOCH', '  ')
    assert source_date_epoch() is None
    monkeypatch.setenv('SOURCE_DATE_EPOCH', str(EPOCH))
    assert source_date_epoch() == EPOCH
    monkeypatch.setenv('SOURCE_DATE_EPOCH', '-1')
    with pytest.raises(ValueError):
        source_date_epoch()
