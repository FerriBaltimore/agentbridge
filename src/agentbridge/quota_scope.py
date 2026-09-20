"""Stable identity for Claude quota pools with model and surface scopes."""
import hashlib
import json


def _text(value):
    return value if isinstance(value, str) and 0 < len(value) <= 512 and all(ord(c) >= 32 for c in value) else None


def _scope(value):
    if not isinstance(value, dict):
        return None, None
    return _text(value.get('id')) or _text(value.get('model')), _text(value.get('display_name'))


def claude_limit(item):
    """Return safe scope metadata, or None for an unidentifiable limit.

Display names are retained as labels, never guessed to be native model IDs.
Provider group and surface matter to identity: two model pools may otherwise
have the same kind and name while limiting different products.
"""
    if not isinstance(item, dict) or not _text(item.get('kind')):
        return None
    kind, group = item['kind'], _text(item.get('group'))
    raw_scope = item.get('scope') if isinstance(item.get('scope'), dict) else {}
    model_id, model_label = _scope(raw_scope.get('model'))
    surface_id, surface_label = _scope(raw_scope.get('surface'))
    model, surface = model_id or model_label, surface_id or surface_label
    alias = {'session': 'five_hour', 'weekly_all': 'seven_day'}.get(kind) if not model and not surface else None
    identity = [kind, group, model, surface]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:24]
    name = alias or 'scoped:' + digest
    scope = ('model_surface' if model and surface else 'model' if model else 'surface' if surface
             else 'account' if alias else 'unknown')
    labels = [label for label in (model_label or model_id, surface_label or surface_id) if label]
    seconds = 18000 if kind == 'session' else 604800 if kind.startswith('weekly') else None
    return {'id': name, 'name': name, 'kind': kind, 'group': group, 'scope': scope,
            'model_id': model_id, 'model_label': model_label,
            'surface_id': surface_id, 'surface_label': surface_label,
            'label': ' / '.join(labels) if labels else kind, 'window_seconds': seconds}
