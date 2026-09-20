"""Maintainer workflow reuses dialects without multiplying implementations."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

from agentbridge.provider_contracts import index

spec = importlib.util.spec_from_file_location('index_provider_release',
    Path(__file__).resolve().parents[1] / 'tools/index_provider_release.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def candidate():
    data = index()
    return data, {**data['bindings'][0], 'version': '0.154.0', 'limitations': []}


def test_unchanged_contract_new_version_is_one_binding_not_new_adapter():
    data, observed = candidate()
    proposed = module.propose(data, observed, observed['contract_id'], 'fixtures/review-123')
    assert len(proposed['contracts']) == len(data['contracts'])
    assert len(proposed['bindings']) == len(data['bindings']) + 1
    assert proposed['bindings'][-1]['contract_id'] == observed['contract_id']
    assert module.propose(proposed, observed, observed['contract_id'], 'fixtures/review-123') == proposed


def test_new_contract_gets_new_content_id_without_editing_old_profile():
    data, observed = candidate()
    manifest = deepcopy(data['contracts'][0]['manifest'])
    manifest['adapter_dialect'] = 2
    proposed = module.propose(data, observed, None, 'fixtures/review-456', manifest)
    assert proposed['contracts'][:3] == data['contracts']
    assert len(proposed['contracts']) == 4
    assert proposed['bindings'][-1]['contract_id'] == proposed['contracts'][-1]['id']


@pytest.mark.parametrize('field,value', [('version', '0.154.0-preview'), ('structural_hash', None),
                                        ('component', 'claude-cli'), ('evidence_kind', 'invented')])
def test_invalid_inspection_cannot_be_indexed(field, value):
    data, observed = candidate()
    observed[field] = value
    with pytest.raises(ValueError):
        module.propose(data, observed, observed['contract_id'], 'fixtures/review')


def test_old_binding_cannot_be_reassigned_and_review_required():
    data, observed = candidate()
    with pytest.raises(ValueError):
        module.propose(data, observed, observed['contract_id'], '')
    observed['version'] = data['bindings'][0]['version']
    observed['structural_hash'] = 'a' * 64
    with pytest.raises(ValueError, match='different binding'):
        module.propose(data, observed, observed['contract_id'], 'fixtures/review')
