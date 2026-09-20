"""Common quota observations. A passed reset is not evidence of renewed capacity."""
from datetime import datetime, timezone
import math
import time

from .quota_scope import claude_limit, legacy_window


def number(value, *, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        valid = math.isfinite(value) and value >= 0 and (maximum is None or value <= maximum)
    except OverflowError:
        valid = False
    if not valid:
        return None
    return value


def text(value):
    return value if isinstance(value, str) and 0 < len(value) <= 512 and all(ord(c) >= 32 for c in value) else None


def timestamp(value):
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        value = number(value)
        if value is not None:
            datetime.fromtimestamp(value, timezone.utc)
        return value
    except (ValueError, OverflowError, OSError):
        return None


def iso(value):
    value = timestamp(value)
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds') if value is not None else None


def window(name, item, *, pool=None, scope='unknown', model=None, family=None, seconds=None, now=None):
    now = time.time() if now is None else now
    used = number(item.get('used_percent', item.get('usedPercent', item.get('utilization', item.get('percent')))))
    reset = timestamp(item.get('resets_at', item.get('resetsAt', item.get('resetAt'))))
    minutes = number(item.get('windowDurationMins', item.get('window_minutes')))
    duration = number(item.get('window_seconds', seconds if minutes is None else minutes * 60))
    return {'id': f'{pool}:{name}' if pool else name, 'name': name, 'pool_id': pool or name,
            'scope': scope, 'model_id': text(model), 'model_family': text(family),
            'label': text(item.get('label')) or text(item.get('display_name')) or name,
            'used_percent': used, 'remaining_percent': max(0, 100 - used) if used is not None else None,
            'window_seconds': duration, 'resets_at': iso(reset),
            'reset_after_seconds': max(0, math.ceil(reset - now)) if reset is not None else None,
            'reset_due': reset <= now if reset is not None else None,
            'limit_reached': used >= 100 if used is not None else None}


def codex(data, now):
    raw = data.get('quota') or data.get('rate_limits') or data.get('limits') or data
    if not isinstance(raw, dict):
        return []
    buckets = raw.get('rateLimitsByLimitId')
    buckets = dict(buckets) if isinstance(buckets, dict) else {}
    legacy = raw.get('rateLimits')
    if isinstance(legacy, dict):
        buckets.setdefault(legacy.get('limitId') or 'codex', legacy)
    if not buckets and any(k in raw for k in ('primary', 'secondary')):
        buckets[raw.get('limit_id') or raw.get('limitId') or 'codex'] = raw
    rows = []
    for pool, bucket in list(buckets.items())[:100]:
        if not text(pool) or not isinstance(bucket, dict):
            continue
        for name in ('primary', 'secondary'):
            item = bucket.get(name)
            if not isinstance(item, dict):
                continue
            value = window(name, item, pool=pool, scope='account_pool', now=now)
            value['pool_label'] = text(bucket.get('limitName'))
            value['rate_limit_reached_type'] = text(bucket.get('rateLimitReachedType'))
            rows.append(value)
    return rows


def claude(data, now):
    """Prefer structural facts; unmatched legacy observations stay supplemental.

An opaque legacy suffix cannot safely be matched to a model pool. Preserve
such observations without implying they are additional independent capacity.
"""
    rows = []
    limits = data.get('limits')
    for item in limits[:100] if isinstance(limits, list) else []:
        metadata = claude_limit(item)
        if metadata is None:
            continue
        name = metadata['name']
        if any(row['name'] == name for row in rows):
            continue
        row = window(name, item, model=metadata['model_id'], scope=metadata['scope'],
                     seconds=metadata['window_seconds'], now=now)
        rows.append({**row, **metadata})
    structured = bool(rows)
    for name, item in list(data.items())[:100]:
        if name == 'extra_usage' or not text(name) or not isinstance(item, dict) or not any(k in item for k in ('utilization', 'percent')):
            continue
        if any(row['name'] == name for row in rows):
            continue
        metadata = legacy_window(name)
        row = window(name, item, seconds=metadata['window_seconds'], scope=metadata['scope'], now=now)
        rows.append({**metadata, **row, 'supplemental': structured})
    return rows


def claude_stream(data, now):
    """Native stream utilization is a fraction; OAuth utilization is a percent."""
    raw = data.get('limits') if isinstance(data.get('limits'), dict) else data
    rows = []
    unified = raw.get('unifiedWindows', raw.get('unified_windows'))
    if isinstance(unified, dict):
        candidates = list(unified.items())[:100]
    elif isinstance(unified, list):
        candidates = [(r.get('rateLimitType', r.get('type')), r) for r in unified[:100] if isinstance(r, dict)]
    else:
        candidates = []
    name = text(raw.get('rateLimitType'))
    if name and not any(key == name for key, _ in candidates):
        candidates.append((name, raw))
    for name, item in candidates:
        if not text(name) or not isinstance(item, dict):
            continue
        used = number(item.get('utilization'))
        metadata = legacy_window(name)
        structural = isinstance(item.get('scope'), dict)
        if structural:
            metadata = claude_limit({**item, 'kind': text(item.get('kind')) or name}) or metadata
        row = window(name, {**item, 'used_percent': used * 100 if used is not None else None},
                     seconds=metadata['window_seconds'], scope=metadata['scope'], now=now)
        row = {**row, **metadata} if structural else {**metadata, **row}
        status = item.get('status', raw.get('status') if name == raw.get('rateLimitType') else None)
        row['status'] = status if status in {'allowed', 'allowed_warning', 'rejected'} else None
        if row['status'] == 'rejected':
            row['limit_reached'] = True
        rows.append(row)
    return rows


def normalize(engine, data, now=None):
    now = time.time() if now is None else now
    streamed = data.get('source') == 'claude_stream' or 'rateLimitType' in data or 'unifiedWindows' in data
    rows = codex(data, now) if engine == 'codex' else (
        claude_stream(data, now) if streamed else claude(data, now)) if engine == 'claude' else []
    if rows:
        return rows
    for item in data.get('windows', []) if isinstance(data.get('windows'), list) else []:
        if not isinstance(item, dict) or not text(item.get('name')):
            continue
        name = item['name']
        metadata = legacy_window(name) if engine == 'claude' else {'scope': 'account_pool', 'window_seconds': None}
        scope, family = item.get('scope', metadata['scope']), item.get('model_family')
        if engine == 'claude' and scope == 'model_family' and item.get('scope_source') != 'provider':
            # Older stored rows inferred families from window suffixes. A read
            # must not perpetuate that association without provider evidence.
            scope, family = 'unknown', None
        normalized = window(name, item, pool=item.get('pool_id'),
            scope=scope, model=item.get('model_id'), family=family,
            seconds=metadata['window_seconds'], now=now)
        if text(item.get('id')):
            normalized['id'] = item['id']
        rows.append({**item, **normalized})
    return rows


def reset_credits(raw, now=None):
    now = time.time() if now is None else now
    if not isinstance(raw, dict):
        return {'status': 'unknown', 'available_count': None, 'credits': None}
    count = raw.get('availableCount')
    if type(count) is not int or count < 0:
        count = None
    details = raw.get('credits')
    rows = None
    if isinstance(details, list):
        rows = []
        for item in details[:1000]:
            if not isinstance(item, dict) or not text(item.get('id')):
                continue
            expires = timestamp(item.get('expiresAt'))
            rows.append({'id': item['id'], 'status': text(item.get('status')),
                         'reset_type': text(item.get('resetType')), 'granted_at': iso(item.get('grantedAt')),
                         'expires_at': iso(expires),
                         'expires_in_seconds': max(0, math.ceil(expires - now)) if expires is not None else None,
                         'expired': expires <= now if expires is not None else None})
    return {'status': 'unknown' if count is None else 'available' if count else 'none',
            'available_count': count, 'credits': rows}


def project(engine, data, now=None):
    """Recompute countdowns on read, including cached observations and event replay."""
    now = time.time() if now is None else now
    result = dict(data)
    rows = normalize(engine, data, now)
    if rows or data.get('supported') or 'windows' in data:
        result['windows'] = rows
    observed = timestamp(data.get('observed_at', data.get('as_of')))
    age = max(0, now - observed) if observed is not None else None
    ttl = 1800 if data.get('source') == 'codex_rollout' else 60
    result['age_seconds'] = round(age, 3) if age is not None else None
    result['stale'] = bool(data.get('stale', data.get('outdated', False)) or observed is None or age >= ttl)
    if any(row['reset_due'] for row in rows):
        result['stale'] = True
    result['schema_version'] = 1
    if engine == 'codex':
        from .quota_spend import codex_spend
        raw = data.get('quota') or data.get('limits') or data
        credits = reset_credits(raw.get('rateLimitResetCredits') if isinstance(raw, dict) else None, now)
        expired = any(row['expired'] and row['status'] in {None, 'available'} for row in credits['credits'] or [])
        result['pools'] = codex_spend(raw, now)
        if any((pool['individual_limit'] or {}).get('reset_due') for pool in result['pools']):
            result['stale'] = True
        result['reset_credits'] = {**credits, 'observed_at': iso(observed),
                                  'age_seconds': result['age_seconds'], 'stale': result['stale'] or expired}
    return result
