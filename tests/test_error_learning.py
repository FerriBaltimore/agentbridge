"""Reviewed classifications preserve failure evidence and never run provider work."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentbridge.error_evidence import capture
from agentbridge.error_learning import Learning
from agentbridge.errors import BridgeError
from agentbridge.provider_errors import normalize
from agentbridge.store import Store


@pytest.fixture
def learning(tmp_path):
    return Learning(Store(tmp_path / 'bridge'))


def unknown(message='New provider capacity policy.', code='new_code'):
    return capture({'code': code, 'message': message})


def propose(learning, *, engine='codex', version='0.153.0', target='provider_unavailable'):
    evidence = unknown()
    case = learning.capture(engine, evidence, provider_version=version)
    item = learning.propose(case['id'], {'status': 'proposed', 'target_code': target})
    return evidence, case, item


def active_rule(learning, **options):
    evidence, case, item = propose(learning, **options)
    validated = learning.validate(item['id'])
    rule = learning.activate(item['id'], validated['revision'])
    return evidence, case, rule


def test_case_capture_deduplicates_durably_and_has_bounded_paging(learning):
    value = unknown()
    original = learning.capture('codex', value, provider_version='0.153.0')
    reopened = Learning(Store(learning.store.root))
    repeated = reopened.capture('codex', value, provider_version='0.153.0')
    assert original['id'] == repeated['id']
    assert repeated['count'] == 2 and repeated['first_seen'] == original['first_seen']
    assert repeated['last_seen'] >= original['last_seen']
    assert repeated['evidence'] == value
    different = reopened.capture('codex', value, provider_version='0.154.0')
    assert different['id'] != original['id']
    first = reopened.list_cases(limit=1)
    assert len(first['items']) == 1 and first['next_cursor'] == 1
    assert reopened.list_cases(limit=1, cursor=first['next_cursor'])['next_cursor'] is None


def test_concurrent_capture_does_not_lose_occurrences(learning):
    value = unknown()
    def observe(_):
        return learning.capture('claude', value)
    with ThreadPoolExecutor(max_workers=4) as pool:
        cases = list(pool.map(observe, range(12)))
    assert len({case['id'] for case in cases}) == 1
    assert learning.get_case(cases[0]['id'])['count'] == 12


@pytest.mark.parametrize('bad', [
    {'message': 'secret-body'},
    {'signals': ['ignore_all_prior_rules']},
    {'shape': ['access_token:string']},
    {'code_fingerprint': 'sk-secret'},
    {'fingerprint': 'not-a-hash'},
    {'http_status': True},
])
def test_unknown_text_cannot_be_persisted_as_evidence(learning, bad):
    with pytest.raises(BridgeError, match='safe structural'):
        learning.capture('codex', {**unknown(), **bad})
    assert learning.list_cases()['items'] == []


@pytest.mark.parametrize('version', ['secret-token', '1.2.3-secret', '', 123, True])
def test_arbitrary_provider_versions_are_not_stored(learning, version):
    with pytest.raises(BridgeError):
        learning.capture('codex', unknown(), provider_version=version)


def test_unknown_secret_native_code_and_body_never_reach_learning_store(learning):
    value = capture({'code': 'unknown-secret-provider-code', 'message': 'secret-password',
                     'authorization': 'secret-token', 'data': {'password': 'secret-value'}})
    case = learning.capture('claude', value)
    learning.propose(case['id'], {'status': 'insufficient_evidence', 'target_code': None})
    with learning.store.connect() as db:
        text = '\n'.join(db.iterdump())
    assert 'secret' not in text
    assert 'unknown-secret-provider-code' not in json.dumps(case)


@pytest.mark.parametrize('result', [
    {'status': 'proposed', 'target_code': 'execute_shell'},
    {'status': 'proposed', 'target_code': 'quota_exhausted', 'rationale': 'secret-body'},
    {'status': 'proposed', 'target_code': 'quota_exhausted', 'retryable': True},
    {'status': 'active', 'target_code': 'quota_exhausted'},
    {'status': 'insufficient_evidence', 'target_code': 'quota_exhausted'},
])
def test_proposals_accept_no_generated_code_policy_or_raw_rationale(learning, result):
    case = learning.capture('claude', unknown())
    with pytest.raises(BridgeError) as error:
        learning.propose(case['id'], result)
    assert error.value.code == 'invalid_error_proposal'


def test_ai_provenance_is_an_operation_reference_only(learning):
    case = learning.capture('claude', unknown())
    result = {'status': 'insufficient_evidence', 'target_code': None}
    item = learning.propose(case['id'], result, provenance={
        'source': 'ai_session', 'operation_id': 'a' * 32})
    assert item['provenance'] == {'source': 'ai_session', 'operation_id': 'a' * 32}
    for provenance in ({'source': 'ai_session'}, {'source': 'ai_session', 'operation_id': 'secret'},
                       {'source': 'manual', 'rationale': 'secret-body'}):
        with pytest.raises(BridgeError):
            learning.propose(case['id'], result, provenance=provenance)
    with pytest.raises(BridgeError) as error:
        learning.validate(item['id'])
    assert error.value.code == 'error_proposal_not_ready'


def test_activation_requires_validation_and_matching_review_revision(learning):
    evidence, _, item = propose(learning)
    original = normalize('codex', {}, outcome='unknown')
    assert learning.apply('codex', evidence, original, '0.153.0') == original
    with pytest.raises(BridgeError) as error:
        learning.activate(item['id'], item['revision'])
    assert error.value.code == 'error_proposal_not_ready'
    validated = learning.validate(item['id'])
    assert validated['validation']['kind'] == 'structural_fixture'
    assert validated['validation']['semantic_verification'] is False
    assert validated['validation']['checks'] >= 12
    assert learning.apply('codex', evidence, original, '0.153.0') == original
    with pytest.raises(BridgeError) as error:
        learning.activate(item['id'], item['revision'])
    assert error.value.code == 'version_conflict'
    rule = learning.activate(item['id'], validated['revision'])
    assert rule['state'] == 'active' and rule['revision'] == 1


def test_learned_cause_preserves_unknown_outcome_and_native_retry_evidence(learning):
    evidence, _, rule = active_rule(learning, target='quota_exhausted')
    issue = normalize('codex', {}, outcome='unknown', terminal=False, provider_retrying=True)
    changed = learning.apply('codex', evidence, issue, '0.153.0')
    assert changed['code'] == 'quota_exhausted' and changed['category'] == 'quota'
    assert changed['outcome'] == 'unknown' and changed['retryable'] is False
    assert changed['action'] == 'inspect'
    assert changed['terminal'] is False and changed['provider_retrying'] is True
    assert changed['phase'] == issue['phase']
    assert changed['details']['rule_id'] == rule['id']
    assert changed['details']['rule_revision'] == 1
    assert issue['code'] == 'provider_failed'


def test_unclassified_unknown_outcome_can_gain_cause_without_losing_uncertainty(learning):
    evidence, _, _ = active_rule(learning)
    issue = normalize('codex', {}, outcome='unknown')
    issue['code'] = 'unknown_outcome'
    changed = learning.apply('codex', evidence, issue, '0.153.0')
    assert changed['code'] == 'provider_unavailable'
    assert changed['outcome'] == 'unknown' and changed['action'] == 'inspect'


@pytest.mark.parametrize('code', [
    'safety_blocked', 'quota_exhausted', 'authentication_required', 'unknown_outcome', 'interrupted',
])
def test_reviewed_rule_cannot_override_known_native_failures(learning, code):
    evidence, _, _ = active_rule(learning)
    known = normalize('codex', {'code': code}, outcome='unknown')
    assert learning.apply('codex', evidence, known, '0.153.0') == known


def test_matching_is_exact_in_engine_version_and_fingerprint(learning):
    evidence, _, _ = active_rule(learning)
    issue = normalize('codex', {}, outcome='unknown')
    for engine, version, value in (
        ('claude', '0.153.0', evidence), ('codex', None, evidence),
        ('codex', '0.154.0', evidence), ('codex', '0.153.0', unknown('Different response')),
    ):
        assert learning.apply(engine, value, issue, version) == issue
    evidence, _, _ = active_rule(learning, engine='claude', version=None)
    assert learning.apply('claude', evidence, issue)['code'] == 'provider_unavailable'
    assert learning.apply('claude', evidence, issue, '2.1.266') == issue


def test_durable_deactivation_rolls_back_without_deleting_audit(learning):
    evidence, _, rule = active_rule(learning)
    issue = normalize('codex', {}, outcome='unknown')
    reopened = Learning(Store(learning.store.root))
    assert reopened.apply('codex', evidence, issue, '0.153.0')['code'] == 'provider_unavailable'
    with pytest.raises(BridgeError) as error:
        reopened.deactivate(rule['id'], 0)
    assert error.value.code == 'version_conflict'
    inactive = reopened.deactivate(rule['id'], 1)
    assert inactive['state'] == 'inactive' and inactive['revision'] == 2
    assert reopened.apply('codex', evidence, issue, '0.153.0') == issue
    assert reopened.get_rule(rule['id']) == inactive
    with learning.store.connect() as db:
        events = [dict(row) for row in db.execute('SELECT * FROM error_learning_audit ORDER BY sequence')]
    assert [row['operation'] for row in events] == [
        'proposed', 'validated_fixture', 'proposal_activated', 'activated', 'deactivated']
    assert events[-1]['object_id'] == rule['id'] and events[-1]['revision'] == 2


def test_conflicting_active_rule_cannot_be_silently_replaced(learning):
    _, case, _ = active_rule(learning)
    item = learning.propose(case['id'], {'status': 'proposed', 'target_code': 'billing_required'})
    item = learning.validate(item['id'])
    with pytest.raises(BridgeError) as error:
        learning.activate(item['id'], item['revision'])
    assert error.value.code == 'error_rule_conflict'


def test_provenance_and_turn_references_must_exist(learning):
    with pytest.raises(BridgeError):
        learning.capture('codex', unknown(), turn_id='missing-turn')
    assert learning.list_cases()['items'] == []
