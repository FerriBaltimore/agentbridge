"""Write and check dist/agentbridge-<version>.manifest.json for a built wheel and sdist.

    SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) python3 tools/release_manifest.py --build
    python3 tools/release_manifest.py --check

The manifest is what the owner signs and what Fullbrain v2 pins (docs/release.md,
docs/compatibility.md). `protocols` and `grantbridge_min` come from
`agentbridge.protocol_versions`; the digests are computed from the files in `dist/`;
`built_at` follows SOURCE_DATE_EPOCH when it is set. `--build` runs the in-tree PEP 517
backend (tools/bundle_backend.py) with the interpreter's own setuptools, so it needs no
network for build dependencies; `python -m build --no-isolation` is equivalent.
"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / 'tools', ROOT / 'src'):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
from agentbridge.protocol_versions import GRANTBRIDGE_MIN, protocols  # noqa: E402
from reproducible_sdist import source_date_epoch  # noqa: E402

SCHEMA = 1
ARCHES = ('x86_64', 'aarch64')
FULLBRAIN_PIN_NOTE = (
    'Fullbrain v2 does not pin this wheel digest. dev/agentbridge/build_v2.py <checkout> <out> '
    'packs src/agentbridge/**/*.py, *.json and py.typed plus the prepared bundle (lock.json, '
    'assets, GrantBridge sources, licenses) into a deterministic PAX tar (mtime 0) named by '
    'its SHA-256. That digest is PIN_SHA256 in backend/fullbrain/adapters/agentbridge/client.py, '
    'immutable_artifact_sha256 in tests/fixtures/agentbridge_protocol.json and '
    'components.agentbridge.latest_known in deploy/delivery/known-versions.json. Prepare the '
    'x86_64 bundle first (tools/prepare_bundle.py), which also places the native_bwrap asset '
    'and license the v2 builder requires.')
# setuptools rewrites sys.argv inside each hook, so the driver reads its arguments once.
BUILD_DRIVER = ('import sys; tools, dist = sys.argv[1:3]; sys.path.insert(0, tools); '
                'import bundle_backend as backend; print(backend.build_sdist(dist)); '
                'print(backend.build_wheel(dist))')


def digest(path):
    value = sha256()
    with Path(path).open('rb') as source:
        while chunk := source.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def project_metadata(root=ROOT):
    """Name, version and Python range from pyproject.toml; the package must echo the version."""
    project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    source = (root / 'src/agentbridge/__init__.py').read_text()
    declared = re.search(r"^__version__\s*=\s*'([^']+)'", source, re.MULTILINE)
    if declared is None or declared.group(1) != project['version']:
        raise ValueError('agentbridge.__version__ differs from the pyproject.toml version.')
    return {'name': project['name'], 'version': project['version'],
            'python_requires': project['requires-python']}


def git_state(root=ROOT):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    return {'git_commit': git('rev-parse', 'HEAD'),
            'working_tree_dirty': bool(git('status', '--porcelain'))}


def target_arch(value=None):
    arch = (value or os.environ.get('AGENTBRIDGE_TARGET_ARCH') or platform.machine()).lower()
    arch = 'aarch64' if arch == 'arm64' else arch
    if arch not in ARCHES:
        raise ValueError('Choose x86_64 or aarch64 as the wheel architecture.')
    return arch


def distribution_files(dist, name, version, arch):
    stem = re.sub(r'[-_.]+', '_', name)
    wheel = dist / f'{stem}-{version}-py3-none-manylinux_2_28_{arch}.whl'
    sdist = dist / f'{stem}-{version}.tar.gz'
    for path in (wheel, sdist):
        if not path.is_file():
            raise FileNotFoundError(f'{path.name} is missing from {dist}; build it first '
                                    '(--build).')
    return wheel, sdist


def bundle_versions(root=ROOT):
    lock = json.loads((root / 'src/agentbridge/bundle/lock.json').read_text())
    return {'cli_proxy_api': lock['cli_proxy_api']['version'], 'codex': lock['codex']['version'],
            'node': lock['node']['version'], 'grantbridge_commit': lock['grantbridge']['commit']}


def built_at():
    epoch = source_date_epoch()
    moment = (datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None
              else datetime.now(timezone.utc))
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ'), epoch


def manifest_path(dist, version):
    return Path(dist) / f'agentbridge-{version}.manifest.json'


def _artifact(path):
    return {'file': path.name, 'sha256': digest(path), 'bytes': path.stat().st_size}


def write_manifest(dist, *, root=ROOT, arch=None, output=None):
    dist = Path(dist)
    project = project_metadata(root)
    arch = target_arch(arch)
    wheel, sdist = distribution_files(dist, project['name'], project['version'], arch)
    moment, epoch = built_at()
    manifest = {'schema': SCHEMA, **project, **git_state(root), 'platform': f'linux_{arch}',
                'wheel': _artifact(wheel), 'sdist': _artifact(sdist), 'protocols': protocols(),
                'grantbridge_min': GRANTBRIDGE_MIN, 'bundle': bundle_versions(root),
                'fullbrain_pin_note': FULLBRAIN_PIN_NOTE, 'built_at': moment,
                'source_date_epoch': epoch}
    path = Path(output) if output else manifest_path(dist, project['version'])
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    return path, manifest


def _artifact_problems(kind, entry, directory, arch):
    if (not isinstance(entry, dict) or not isinstance(entry.get('file'), str)
            or '/' in entry['file'] or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256')))):
        return [f'{kind} entry is malformed']
    if kind == 'wheel' and not entry['file'].endswith(f'manylinux_2_28_{arch}.whl'):
        return ['wheel file does not match the manifest platform']
    path = directory / entry['file']
    if not path.is_file():
        return [f"{entry['file']} is missing beside the manifest"]
    if path.stat().st_size != entry.get('bytes') or digest(path) != entry['sha256']:
        return [f"{entry['file']} differs from its recorded size or digest"]
    return []


def validate_manifest(path, *, root=ROOT):
    """Check a manifest against the files beside it and this checkout; raise ValueError."""
    path = Path(path)
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict):
        raise ValueError('The manifest is not a JSON object.')
    problems = ['schema is unsupported'] if manifest.get('schema') != SCHEMA else []
    expected = {**project_metadata(root), 'protocols': protocols(),
                'grantbridge_min': GRANTBRIDGE_MIN, 'bundle': bundle_versions(root)}
    problems += [f'{key} differs from the checkout' for key, value in expected.items()
                 if manifest.get(key) != value]
    platform_value = str(manifest.get('platform'))
    arch = platform_value.removeprefix('linux_')
    if not platform_value.startswith('linux_') or arch not in ARCHES:
        problems.append('platform is not a supported Linux architecture')
    for kind in ('wheel', 'sdist'):
        problems += _artifact_problems(kind, manifest.get(kind), path.parent, arch)
    if not re.fullmatch(r'[0-9a-f]{40}', str(manifest.get('git_commit'))):
        problems.append('git_commit is not a commit id')
    if not isinstance(manifest.get('working_tree_dirty'), bool):
        problems.append('working_tree_dirty must be a boolean')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', str(manifest.get('built_at'))):
        problems.append('built_at is not an ISO 8601 UTC timestamp')
    if not isinstance(manifest.get('fullbrain_pin_note'), str):
        problems.append('fullbrain_pin_note is missing')
    if problems:
        raise ValueError('; '.join(problems))
    return manifest


def build_distributions(dist, *, root=ROOT):
    """Build the sdist and the wheel through tools/bundle_backend.py in a child interpreter."""
    try:
        import setuptools
    except ImportError:
        raise RuntimeError('setuptools >= 70.1 is required in this interpreter; '
                           'python -m build --no-isolation is an equivalent path.') from None
    major, minor = (int(part) for part in setuptools.__version__.split('.')[:2])
    if (major, minor) < (70, 1):
        raise RuntimeError(f'setuptools {setuptools.__version__} is older than 70.1.')
    shutil.rmtree(root / 'build', ignore_errors=True)
    dist = Path(dist)
    dist.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-c', BUILD_DRIVER, str(root / 'tools'), str(dist)]
    result = subprocess.run(command, cwd=root, timeout=3600)
    if result.returncode != 0:
        raise RuntimeError(f'The distribution build exited with status {result.returncode}.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dist', type=Path, default=ROOT / 'dist')
    parser.add_argument('--arch', choices=ARCHES, help='Wheel architecture (default: host)')
    parser.add_argument('--output', type=Path, help='Manifest path (default: dist/agentbridge-'
                                                      '<version>.manifest.json)')
    parser.add_argument('--build', action='store_true',
                        help='Build the sdist and wheel first through tools/bundle_backend.py')
    parser.add_argument('--check', action='store_true',
                        help='Validate the existing manifest instead of writing one')
    args = parser.parse_args(argv)
    try:
        if args.check:
            path = args.output or manifest_path(args.dist, project_metadata()['version'])
            manifest = validate_manifest(path)
            print(f"{path.name} is valid: wheel {manifest['wheel']['sha256']} "
                  f"sdist {manifest['sdist']['sha256']} commit {manifest['git_commit']}")
            return 0
        if args.build:
            build_distributions(args.dist)
        path, manifest = write_manifest(args.dist, arch=args.arch, output=args.output)
        print(json.dumps({'manifest': str(path), 'version': manifest['version'],
                          'git_commit': manifest['git_commit'],
                          'working_tree_dirty': manifest['working_tree_dirty'],
                          'wheel_sha256': manifest['wheel']['sha256'],
                          'sdist_sha256': manifest['sdist']['sha256']}, sort_keys=True))
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        print(f'release_manifest: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
