"""Pin a complete, verified GrantBridge runtime; never download or publish anything."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

from import_grantbridge import digest, safe_path

ROOT = Path(__file__).resolve().parents[1]


def import_runtime(archive, manifest_path, expected, *, root=ROOT):
    if digest(archive) != expected:
        raise ValueError('The runtime differs from the trusted digest.')
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest.get('name') != 'grantbridge' or manifest.get('tgz_sha256') != expected:
        raise ValueError('The runtime manifest is invalid.')
    with tarfile.open(archive, 'r:gz') as packed:
        found = {}
        for member in packed:
            name = safe_path(member.name)
            if not member.isfile() or name in found or member.size > 32 * 1024 * 1024:
                raise ValueError('The runtime contains an invalid member.')
            with packed.extractfile(member) as source:
                found[name] = sha256(source.read()).hexdigest()
        if found != manifest['files']:
            raise ValueError('The runtime file set differs from its manifest.')
    package = root / 'src/agentbridge/bundle'
    lock_path = package / 'lock.json'
    lock = json.loads(lock_path.read_text())
    entry = {'version': manifest['version'], 'commit': manifest['git_commit'],
             'artifact_sha256': expected, 'runtime_sha256': expected,
             'source_sha256': expected, 'ephemeral_browser_state': True}
    if manifest.get('working_tree'):
        entry['working_tree'] = True
    # Validate extraction with the same runtime reader used by installed wheels.
    import sys
    sys.path.insert(0, str(root / 'src'))
    from agentbridge.bundle.grantbridge_archive import extract_runtime

    with tempfile.TemporaryDirectory(prefix='aa-auth-sdk-import-') as directory:
        stage = Path(directory)
        (stage / 'assets').mkdir()
        shutil.copyfile(archive, stage / 'assets/grantbridge.tar.gz')
        extract_runtime(stage, stage / 'unpacked', entry)
    (package / 'assets').mkdir(exist_ok=True)
    shutil.copyfile(archive, package / 'assets/grantbridge.tar.gz')
    lock['grantbridge'] = entry
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + '\n')
    return entry


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--expected-sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(import_runtime(args.archive, args.manifest, args.expected_sha256), indent=2))
