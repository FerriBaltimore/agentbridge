"""Propose a reviewed exact release binding for normal repository delivery.

This is a maintainer tool, never a runtime activation API. Inspect first, run
conformance tests, then pass a review reference and an explicit contract choice.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agentbridge.provider_contracts import digest, index


def propose(current, inspection, contract_id, review_reference, manifest=None):
    result = deepcopy(current)
    engine = inspection.get('engine')
    version = inspection.get('version')
    if engine not in {'codex', 'claude', 'cursor'} or not isinstance(version, str) or not re.fullmatch(r'\d{1,5}\.\d{1,5}\.\d{1,5}', version):
        raise ValueError('An exact observed provider release is required.')
    component = 'cursor-sdk' if engine == 'cursor' else engine + '-cli'
    if inspection.get('component') != component or 'bundled_sdk_version_mismatch' in inspection.get('limitations', []):
        raise ValueError('Provider components do not match.')
    if (not isinstance(review_reference, str) or not 1 <= len(review_reference) <= 256
            or any(ord(c) < 32 or ord(c) == 127 for c in review_reference)):
        raise ValueError('A bounded conformance review reference is required.')
    if manifest is not None:
        if manifest.get('engine') != engine or manifest.get('format_version') != 1:
            raise ValueError('The new manifest must identify the same provider.')
        profile = {'id': 'sha256:' + digest(manifest), 'manifest': manifest}
        if contract_id and contract_id != profile['id']:
            raise ValueError('Contract content and ID differ.')
        contract_id = profile['id']
        if not any(item['id'] == contract_id for item in result['contracts']):
            result['contracts'].append(profile)
    profile = next((item for item in result['contracts'] if item['id'] == contract_id), None)
    if profile is None or profile['manifest']['engine'] != engine:
        raise ValueError('Choose an existing provider contract or supply a new manifest.')
    evidence_hash = inspection.get('structural_hash')
    evidence_kind = inspection.get('evidence_kind')
    if not isinstance(evidence_hash, str) or not re.fullmatch('[a-f0-9]{64}', evidence_hash):
        raise ValueError('A complete offline inspection is required.')
    expected = {'codex': 'codex_json_schema_default_v1', 'claude': 'cli_help_observation_v1',
                'cursor': 'cursor_python_ast_bridge_v1'}[engine]
    if evidence_kind != expected:
        raise ValueError('Unknown evidence format requires adapter review.')
    binding = {'engine': engine, 'component': component, 'version': version,
               'contract_id': contract_id, 'review': review_reference,
               'evidence_kind': evidence_kind, 'structural_hash': evidence_hash}
    existing = next((b for b in result['bindings'] if b['engine'] == engine and b['version'] == version), None)
    if existing:
        if any(existing.get(key) != binding[key] for key in ('contract_id', 'evidence_kind', 'structural_hash')):
            raise ValueError('This exact release already has a different binding; review an index migration.')
        return result
    result['bindings'].append(binding)
    return result


def read_json(path):
    with Path(path).open('rb') as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError('Input exceeds the maintainer tool bound.')
    return json.loads(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspection', required=True)
    parser.add_argument('--contract-id')
    parser.add_argument('--manifest')
    parser.add_argument('--review-reference', required=True)
    parser.add_argument('--write', action='store_true', help='Write the source index for normal test/review/delivery')
    args = parser.parse_args()
    result = propose(index(), read_json(args.inspection), args.contract_id, args.review_reference,
                     read_json(args.manifest) if args.manifest else None)
    text = json.dumps(result, indent=2) + '\n'
    if args.write:
        target = Path(__file__).resolve().parents[1] / 'src/agentbridge/provider_contract_index.json'
        temporary = target.with_suffix('.json.tmp')
        temporary.write_text(text)
        temporary.replace(target)
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
