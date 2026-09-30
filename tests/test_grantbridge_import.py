"""One verified upstream artifact supplies the whole dependency-free login adapter."""

from hashlib import sha256
from io import BytesIO
import json
import tarfile

import pytest

from tools.import_grantbridge import ENTRY, derive, write_closure


def artifact(tmp_path, *, module=None):
    files = {'package.json': json.dumps({'name': 'grantbridge', 'version': '1.0.0-rc.3'}),
             'LICENSE': 'MIT fixture', ENTRY: module or (
                 "import { createInterface } from 'node:readline';\n"
                 "import { value } from '../src/proxy.mjs';\n"),
             'src/proxy.mjs': 'export const value = 1;\n',
             'node_modules/unneeded/index.js': 'not bundled'}
    archive = tmp_path / 'upstream.tgz'
    with tarfile.open(archive, 'w:gz') as packed:
        for name, source in files.items():
            data = source.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            packed.addfile(member, BytesIO(data))
    digest = sha256(archive.read_bytes()).hexdigest()
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'name': 'grantbridge', 'version': '1.0.0-rc.3',
                                    'git_commit': 'a' * 40, 'tgz_sha256': digest,
                                    'files': {name: sha256(data.encode()).hexdigest()
                                              for name, data in files.items()}}))
    return archive, manifest, digest, files


def test_release_provenance_and_import_closure_derive_exact_bytes(tmp_path):
    archive, manifest, digest, files = artifact(tmp_path)
    entry, sources = derive(archive, manifest, expected_sha256=digest)
    assert entry['version'] == '1.0.0-rc.3' and entry['commit'] == 'a' * 40
    assert entry['artifact_sha256'] == digest
    assert set(sources) == {ENTRY, 'src/proxy.mjs', 'LICENSE'}
    assert sources == {name: files[name].encode() for name in sources}
    generated = tmp_path / 'src/agentbridge/bundle'
    generated.mkdir(parents=True)
    (generated / 'lock.json').write_text(json.dumps({'grantbridge': {'files': {}}}))
    write_closure(entry, sources, root=tmp_path)
    assert json.loads((generated / 'lock.json').read_text())['grantbridge'] == entry
    assert all((generated / 'grantbridge' / name).read_bytes() == data
               for name, data in sources.items())
    assert derive(archive, manifest, expected_sha256=digest) == (entry, sources)


@pytest.mark.parametrize('module', [
    "import '../src/missing.mjs';", "import value from 'npm-package';",
    "import('node:fs');", "import '../outside.json' with {type:'json'};",
    "import value from '../../../../escape.mjs';",
])
def test_missing_or_nonstatic_dependency_is_rejected(tmp_path, module):
    archive, manifest, digest, _ = artifact(tmp_path, module=module)
    with pytest.raises(ValueError):
        derive(archive, manifest, expected_sha256=digest)


def test_archive_and_manifest_are_independently_verified(tmp_path):
    archive, manifest, digest, _ = artifact(tmp_path)
    with pytest.raises(ValueError, match='trusted artifact'):
        derive(archive, manifest, expected_sha256='0' * 64)
    value = json.loads(manifest.read_text())
    value['files'][ENTRY] = '0' * 64
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='manifest digest'):
        derive(archive, manifest, expected_sha256=digest)
