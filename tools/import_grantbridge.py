"""Derive the private proxy adapter from the exact verified source-broker release archive.

The default is read-only. --write replaces the generated closure and its lock provenance.
The caller supplies the trusted artifact digest; a self-consistent manifest alone is not trust.
Node parses modules without evaluating them. Dynamic imports and CommonJS are rejected.
"""

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ENTRY = 'scripts/agentbridge-proxy-adapter.mjs'
HASH = re.compile(r'[0-9a-f]{64}\Z')
PARSE = """
import { readFileSync } from 'node:fs';
import { isBuiltin } from 'node:module';
import { SourceTextModule } from 'node:vm';
const source = JSON.parse(readFileSync(0, 'utf8'));
const module = new SourceTextModule(source);
process.stdout.write(JSON.stringify(module.dependencySpecifiers.map(
  value => ({ value, builtin: value.startsWith('node:') && isBuiltin(value) }))));
"""


def digest(path):
    result = sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def safe_path(value):
    path = PurePosixPath(value)
    if (not isinstance(value, str) or str(path) != value or path.is_absolute()
            or not path.parts or any(part in {'.', '..'} for part in path.parts)
            or '\\' in value):
        raise ValueError('The artifact contains an unsafe path.')
    return value


def dependencies(data, *, node):
    source = data.decode('utf-8')
    # Conservative rejection also covers occurrences inside comments. A closure that needs
    # dynamic loading must first receive an explicitly reviewed packaging implementation.
    gap = r'(?:\s|/\*[\s\S]*?\*/|//[^\n]*\n)*'
    if re.search(r'\b(?:import|require|eval)' + gap + r'\(', source) or 'createRequire' in source:
        raise ValueError('The proxy closure must use static ESM imports only.')
    result = subprocess.run([str(node), '--experimental-vm-modules', '--input-type=module',
                             '-e', PARSE], input=json.dumps(source), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    if result.returncode:
        raise ValueError('Node could not parse the proxy module.')
    return json.loads(result.stdout)


def derive(archive, manifest_path, *, expected_sha256, node='node'):
    if not HASH.fullmatch(expected_sha256) or digest(archive) != expected_sha256:
        raise ValueError('The archive differs from the trusted artifact digest.')
    manifest = json.loads(Path(manifest_path).read_text())
    if (manifest.get('name') != 'grantbridge' or manifest.get('tgz_sha256') != expected_sha256
            or not re.fullmatch(r'[0-9a-f]{40}', str(manifest.get('git_commit')))
            or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?',
                                str(manifest.get('version')))
            or not isinstance(manifest.get('files'), dict)):
        raise ValueError('The GrantBridge release manifest is invalid.')
    with tarfile.open(archive, 'r:gz') as packed:
        members = {}
        for member in packed.getmembers():
            name = safe_path(member.name)
            if not member.isfile() or name in members:
                raise ValueError('The artifact must contain unique regular files only.')
            members[name] = member
        if set(members) != set(manifest['files']):
            raise ValueError('The artifact file set differs from its manifest.')
        for name, member in members.items():
            expected = manifest['files'][name]
            if not isinstance(expected, str) or not HASH.fullmatch(expected):
                raise ValueError('The manifest contains an invalid file digest.')
            with packed.extractfile(member) as stream:
                hashed = sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    hashed.update(block)
            if hashed.hexdigest() != expected:
                raise ValueError('An artifact file differs from its manifest digest.')
        package = json.loads(packed.extractfile(members['package.json']).read())
        if package.get('name') != 'grantbridge' or package.get('version') != manifest['version']:
            raise ValueError('The archived package version differs from its manifest.')
        sources, pending = {}, [ENTRY, 'LICENSE']
        while pending:
            name = pending.pop()
            if name in sources:
                continue
            if name not in members or members[name].size > 2 * 1024 * 1024:
                raise ValueError('A proxy dependency is missing or exceeds the source limit.')
            sources[name] = packed.extractfile(members[name]).read()
            if not name.endswith('.mjs'):
                continue
            for dependency in dependencies(sources[name], node=node):
                value = dependency['value']
                if dependency['builtin']:
                    continue
                if not value.startswith(('./', '../')):
                    raise ValueError('The proxy closure cannot contain npm or network imports.')
                target = safe_path(posixpath.normpath(posixpath.join(
                    posixpath.dirname(name), value)))
                if not target.endswith('.mjs') or not target.startswith(('src/', 'scripts/')):
                    raise ValueError('The proxy closure contains an unsupported dependency.')
                pending.append(target)
    hashes = {name: sha256(data).hexdigest() for name, data in sorted(sources.items())}
    closure = sha256()
    for name, value in hashes.items():
        closure.update(name.encode() + b'\0' + value.encode() + b'\n')
    entry = {'version': manifest['version'], 'commit': manifest['git_commit'],
             'artifact_sha256': expected_sha256, 'files': hashes,
             'source_sha256': closure.hexdigest()}
    return entry, sources


def write_closure(entry, sources, *, root=ROOT):
    bundle = root / 'src/agentbridge/bundle'
    lock_path = bundle / 'lock.json'
    lock = json.loads(lock_path.read_text())
    generated = bundle / 'grantbridge'
    for name in set(lock['grantbridge'].get('files', {})) | set(sources):
        target = generated / safe_path(name)
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise ValueError('The generated bundle destination contains a symbolic link.')
    for name, data in sources.items():
        target = generated / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for name in set(lock['grantbridge'].get('files', {})) - set(sources):
        (generated / name).unlink(missing_ok=True)
    lock['grantbridge'] = entry
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--node', default='node')
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    entry, sources = derive(args.archive, args.manifest, expected_sha256=args.expected_sha256,
                            node=args.node)
    if args.write:
        write_closure(entry, sources)
    print(json.dumps(entry, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
