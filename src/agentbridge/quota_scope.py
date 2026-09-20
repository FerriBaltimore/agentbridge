"""Provider-reported Claude scopes and temporal legacy-window compatibility.

Model and family names are data. A legacy suffix never proves which models
share a quota. Labels are presentation metadata, not native model identifiers.
"""
import hashlib
import json
import math
import re


_COUNTS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
           'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'twelve': 12}
_UNITS = {'minute': 60, 'hour': 3600, 'day': 86400, 'week': 604800}
_TEMPORAL_NAME = re.compile(r'^(\d{1,5}|' + '|'.join(_COUNTS) + r')_(minute|hour|day|week)s?(?:_.+)?$')


def _text(value):
    return value if isinstance(value, str) and 0 < len(value) <= 512 and all(ord(c) >= 32 and ord(c) != 127 for c in value) else None


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        return value if math.isfinite(value) and value > 0 else None
    except OverflowError:
        return None


def legacy_window(name):
    """Infer a period from temporal grammar, never a family from its suffix."""
    match = _TEMPORAL_NAME.fullmatch(name) if _text(name) else None
    seconds = None
    if match:
        count = int(match[1]) if match[1].isdigit() else _COUNTS[match[1]]
        seconds = count * _UNITS[match[2]] if count else None
    return {'window_seconds': seconds,
            'scope': 'account' if name in {'five_hour', 'seven_day'} else 'unknown',
            'scope_source': 'legacy_window_name', 'model_family': None}


def _dimension(value):
    if isinstance(value, str):
        return _text(value), None
    if not isinstance(value, dict):
        return None, None
    return _text(value.get('id')) or _text(value.get('model')), _text(value.get('display_name'))


def _duration(item, kind):
    for key in ('window_seconds', 'window_duration_seconds', 'duration_seconds'):
        if key in item:
            return _positive(item[key]), 'provider'
    raw = item.get('window')
    if isinstance(raw, dict) and 'duration_seconds' in raw:
        return _positive(raw['duration_seconds']), 'provider'
    seconds = legacy_window(kind)['window_seconds']
    if seconds is None:
        seconds = 18000 if kind == 'session' else 604800 if isinstance(kind, str) and (kind == 'weekly' or kind.startswith('weekly_')) else None
    return seconds, 'provider_kind' if seconds is not None else None


def claude_limit(item):
    """Normalize structural limits, including previously unseen model families.

Scope kind, group, model and product participate in identity. Explicit IDs
outlive label changes; without IDs, the bounded reported labels distinguish
pools. Usage, reset deadlines and labels with a stable ID do not change IDs.
"""
    if not isinstance(item, dict):
        return None
    kind, group = _text(item.get('kind')), _text(item.get('group'))
    raw_scope = item.get('scope') if isinstance(item.get('scope'), dict) else {}
    scope_kind = _text(raw_scope.get('kind'))
    provider_id = _text(item.get('id'))
    if kind is None and provider_id is None and scope_kind is None:
        return None
    model_id, model_label = _dimension(raw_scope.get('model'))
    surface_id, surface_label = _dimension(raw_scope.get('surface'))
    family_id, family_label = _dimension(raw_scope.get('model_family'))
    model, surface, family = model_id or model_label, surface_id or surface_label, family_id or family_label
    seconds, duration_source = _duration(item, kind)
    alias = None
    if not model and not surface and not family and not group and scope_kind in {None, 'account', 'all'}:
        alias = {'session': 'five_hour', 'weekly_all': 'seven_day'}.get(kind)
        if alias and duration_source == 'provider' and seconds != legacy_window(alias)['window_seconds']:
            alias = None
    identity = [kind, group, model, surface]
    if scope_kind is not None or family is not None:
        identity.extend([scope_kind, family])
    if duration_source == 'provider':
        identity.append(seconds)
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:24]
    name = provider_id or alias or 'scoped:' + digest
    scope = ('model_surface' if model and surface else 'model' if model else 'model_family' if family else
             'surface' if surface else 'account' if alias or scope_kind in {'account', 'all'} else 'unknown')
    labels = [label for label in (model_label or model_id, family_label or family_id,
                                  surface_label or surface_id) if label]
    return {'id': name, 'name': name, 'kind': kind, 'group': group, 'scope': scope,
            'scope_kind': scope_kind, 'scope_source': 'provider',
            'model_id': model_id, 'model_label': model_label, 'model_family': family_id,
            'model_family_label': family_label, 'surface_id': surface_id, 'surface_label': surface_label,
            'label': _text(item.get('label')) or ' / '.join(labels) or kind or scope_kind or name,
            'window_seconds': seconds, 'window_duration_source': duration_source}
