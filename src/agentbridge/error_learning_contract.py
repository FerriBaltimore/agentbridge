"""Closed evidence and proposal contracts for reviewed error classifications."""
import re

from .errors import BridgeError
from .models import ENGINES


def invalid():
    raise BridgeError('invalid_error_evidence', 'Error evidence must use the safe evidence contract.')


def identity(engine, provider_version):
    if engine not in ENGINES:
        invalid()
    if provider_version is not None and (
        not isinstance(provider_version, str)
        or re.fullmatch(r'[0-9]{1,5}(?:\.[0-9]{1,5}){0,3}', provider_version) is None
    ):
        invalid()
    return engine, provider_version


def proposal(value):
    from .provider_errors import CANONICAL
    if not isinstance(value, dict) or set(value) != {'status', 'target_code'}:
        raise BridgeError('invalid_error_proposal', 'Use only status and a canonical target_code.')
    status, code = value['status'], value['target_code']
    if status == 'insufficient_evidence' and code is None:
        return dict(value)
    if status != 'proposed' or not isinstance(code, str) or code not in CANONICAL:
        raise BridgeError('invalid_error_proposal', 'Use a canonical target_code or insufficient_evidence.')
    return dict(value)


def classify(issue, rule):
    """A learned cause cannot authorize retries or change execution evidence."""
    if (rule['state'] != 'active' or not isinstance(issue, dict)
            or issue.get('details', {}).get('detection') != 'unclassified'):
        return issue
    category = BridgeError(rule['target_code'], 'Reviewed error classification.').safe_data()['category']
    return {**issue, 'code': rule['target_code'], 'category': category,
            'retryable': False, 'action': 'inspect', 'details': {
                **issue.get('details', {}), 'detection': 'learned_rule',
                'unclassified_code': issue['code'],
                'rule_id': rule['id'], 'rule_revision': rule['revision']}}
