"""Bounded, nonverbatim evidence for unknown provider failures."""
import hashlib
import json
import re

from .errors import BridgeError


SIGNALS = frozenset(('billing', 'quota', 'capacity', 'credit', 'exhausted', 'maintenance',
    'concurrency', 'permission', 'authentication', 'expired', 'invalid', 'model', 'context',
    'network', 'rejected', 'unavailable', 'safety', 'policy', 'timeout'))
CODE_KEYS = ('codexErrorInfo', 'proto_error_code', 'sdkErrorCode', 'code', 'type',
             'subtype', 'terminal_reason')
TEXT_KEYS = ('message', 'error', 'errors', 'result', 'additionalDetails', 'text')
STATUS_KEYS = ('status', 'status_code', 'httpStatusCode', 'api_error_status')
KEYS = frozenset((*CODE_KEYS, *TEXT_KEYS, *STATUS_KEYS, 'data'))
SHAPES = frozenset(key + ':' + kind for key in KEYS for kind in
                   ('object', 'array', 'string', 'number', 'boolean', 'null'))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_evidence(value):
    required = {'fingerprint', 'code_fingerprint', 'signals', 'shape', 'http_status'}
    valid = isinstance(value, dict) and set(value) == required
    if valid:
        for name in ('fingerprint', 'code_fingerprint'):
            item = value[name]
            if name == 'code_fingerprint' and item is None:
                continue
            valid = valid and isinstance(item, str) and bool(re.fullmatch('[a-f0-9]{64}', item))
        for name, allowed in (('signals', SIGNALS), ('shape', SHAPES)):
            items = value[name]
            valid = valid and isinstance(items, list) and len(items) <= len(allowed)
            valid = valid and all(isinstance(item, str) and item in allowed for item in items)
        status = value['http_status']
        valid = valid and (status is None or
            isinstance(status, int) and not isinstance(status, bool) and 400 <= status < 600)
    if not valid:
        raise BridgeError('invalid_error_evidence', 'Error evidence must contain safe structural fields only.')
    return {**value, 'signals': sorted(set(value['signals'])), 'shape': sorted(set(value['shape']))}


def capture(value):
    """Hash bounded recognized fields; arbitrary codes and bodies never leave memory."""
    shape, codes, texts, signals, statuses = set(), [], [], set(), []
    remaining = [16384]

    def walk(item, depth=0, key=None):
        if depth > 4 or remaining[0] <= 0:
            return
        kind = ('object' if isinstance(item, dict) else 'array' if isinstance(item, list)
                else 'string' if isinstance(item, str) else 'boolean' if isinstance(item, bool)
                else 'number' if isinstance(item, (int, float)) else 'null')
        if key in KEYS:
            shape.add(key + ':' + kind)
        if isinstance(item, str):
            text = item[:remaining[0]]
            remaining[0] -= len(text)
            signals.update(set(re.findall('[a-z]+', text.lower())) & SIGNALS)
            (codes if key in CODE_KEYS else texts).append(text)
        elif isinstance(item, dict):
            for child_key in sorted(KEYS):
                if child_key in item:
                    walk(item[child_key], depth + 1, child_key)
            # Native enum variants encode their error code as an object key.
            if key == 'codexErrorInfo':
                codes.extend(str(name)[:128] for name in list(item)[:8])
        elif isinstance(item, list):
            for child in item[:16]:
                walk(child, depth + 1, key)
        elif key in STATUS_KEYS and isinstance(item, int) and not isinstance(item, bool):
            if 400 <= item < 600:
                statuses.append(item)

    walk(value)
    return {'fingerprint': _hash([codes, texts, sorted(shape), statuses]),
            'code_fingerprint': _hash(codes) if codes else None,
            'signals': sorted(signals), 'shape': sorted(shape),
            'http_status': statuses[0] if statuses else None}


def from_issue(issue):
    details = issue.get('details')
    evidence = details.get('unknown_evidence') if isinstance(details, dict) else None
    try:
        return validate_evidence(evidence)
    except BridgeError:
        return None
