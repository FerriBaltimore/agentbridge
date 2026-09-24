"""Normalize bounded proxy quota evidence without retaining provider bodies."""

from datetime import datetime, timezone
import math
import re
import time

from ..quota_windows import normalize as normalize_native, text


_CODEX_HEADER = re.compile(
    r'^x-codex-(?:(?P<pool>[a-z0-9_-]+)-)?(?P<part>primary|secondary)-'
    r'(?P<field>used-percent|window-minutes|reset-at|reset-after-seconds)$')
_CLAUDE_HEADER = re.compile(
    r'^anthropic-ratelimit-unified-(?P<claim>[a-z0-9_-]+)-'
    r'(?P<field>utilization|reset|status)$')
_CLAUDE_PERIOD = re.compile(r'^(?P<count>[1-9][0-9]{0,3})(?P<unit>h|d)(?:_.+)?$')


def _number(value, *, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        return None
    return number


def _instant(value):
    if isinstance(value, str) and len(value) <= 64:
        numeric = _number(value)
        if numeric is not None:
            value = numeric
        else:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is not None:
                    return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            except ValueError:
                return None
    numeric = _number(value, maximum=253402300799)
    if numeric is None:
        return None
    try:
        return datetime.fromtimestamp(numeric, timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, OSError, OverflowError):
        return None


def _reset(fields, observed_at):
    direct = _instant(fields.get('reset-at', fields.get('reset_at', fields.get('resetAt'))))
    if direct:
        return direct
    offset = _number(fields.get('reset-after-seconds', fields.get('reset_after_seconds')),
                     maximum=315360000)
    observed = _instant(observed_at)
    if offset is None or observed is None:
        return None
    stamp = datetime.fromisoformat(observed.replace('Z', '+00:00')).timestamp()
    return _instant(stamp + offset)


def _row(identifier, label, scope, model_id, used_percent, window_seconds,
         resets_at, observed_at, *, status=None):
    used = _number(used_percent, maximum=1000)
    duration = _number(window_seconds, minimum=1, maximum=315360000)
    observed = _instant(observed_at)
    label = label if isinstance(label, str) and 0 < len(label) <= 160 and all(ord(c) >= 32 for c in label) else identifier
    row = {'id': identifier, 'label': label, 'scope': scope, 'model_id': model_id,
           'used_percent': used,
           'remaining_percent': max(0, 100 - used) if used is not None else None,
           'window_seconds': int(duration) if duration is not None else None,
           'resets_at': _instant(resets_at), 'observed_at': observed}
    if status in {'allowed', 'allowed_warning', 'rejected'}:
        row['status'] = status
    return row


def _signals(raw):
    if not isinstance(raw, dict):
        return {}, None
    values = raw.get('signals')
    if not isinstance(values, dict):
        return {}, _instant(raw.get('observed_at'))
    clean = {}
    for key, value in list(values.items())[:100]:
        if isinstance(key, str) and isinstance(value, str) and len(key) <= 200 and len(value) <= 512:
            clean[key.lower()] = value
    return clean, _instant(raw.get('observed_at'))


def passive_codex(raw, *, model_id=None):
    values, observed = _signals(raw)
    groups = {}
    for key, value in values.items():
        match = _CODEX_HEADER.fullmatch(key)
        if match:
            pool = match['pool'] or 'base'
            groups.setdefault((pool, match['part']), {})[match['field']] = value
    rows = []
    for (pool, part), fields in sorted(groups.items()):
        used = _number(fields.get('used-percent'), maximum=100)
        if used is None:
            continue
        minutes = _number(fields.get('window-minutes'), minimum=1, maximum=5256000)
        reset = _reset(fields, observed)
        namespace = f'x-codex-{pool}-limit-name'
        label = values.get(namespace) if pool != 'base' else None
        if not isinstance(label, str) or not label or len(label) > 128:
            label = pool if pool != 'base' else part
        if pool != 'base':
            label = f'{label} · {part}'
        scope = 'model' if model_id else 'account' if pool == 'base' else 'unknown'
        rows.append(_row(f'{model_id or "account"}:{pool}:{part}', label, scope,
                         model_id, used, minutes * 60 if minutes else None,
                         reset, observed))
    return rows


def passive_claude(raw, *, model_id=None):
    values, observed = _signals(raw)
    groups = {}
    for key, value in values.items():
        match = _CLAUDE_HEADER.fullmatch(key)
        if match:
            groups.setdefault(match['claim'], {})[match['field']] = value
    rows = []
    for claim, fields in sorted(groups.items()):
        utilization = _number(fields.get('utilization'), maximum=10)
        if utilization is None:
            continue
        period = _CLAUDE_PERIOD.fullmatch(claim)
        duration = int(period['count']) * (3600 if period['unit'] == 'h' else 86400) if period else None
        scope = 'model' if model_id else 'account' if claim in {'5h', '7d'} else 'unknown'
        rows.append(_row(f'{model_id or "account"}:{claim}', claim, scope, model_id,
                         utilization * 100, duration, fields.get('reset'), observed,
                         status=fields.get('status')))
    return rows


def passive(provider, account_quota, model_quotas, models):
    parser = passive_codex if provider == 'codex' else passive_claude if provider == 'claude' else None
    if parser is None:
        return []
    rows = parser(account_quota)
    if not isinstance(model_quotas, dict):
        return rows
    for model in sorted(models):
        if model in model_quotas:
            rows.extend(parser(model_quotas[model], model_id=model))
    return rows[:200]


def project(rows, *, now=None, ttl=60):
    """Recompute freshness at read time; a passed reset is unknown capacity."""
    now = time.time() if now is None else now
    result = []
    for item in rows[:200] if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        observed = _instant(item.get('observed_at'))
        reset = _instant(item.get('resets_at'))
        observed_epoch = (datetime.fromisoformat(observed.replace('Z', '+00:00')).timestamp()
                          if observed else None)
        reset_epoch = (datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp()
                       if reset else None)
        age = now - observed_epoch if observed_epoch is not None else None
        expiry = min(observed_epoch + ttl, reset_epoch) if observed_epoch is not None and reset_epoch is not None else (
            observed_epoch + ttl if observed_epoch is not None else None)
        stale = (age is None or age < 0 or age >= ttl or
                 reset_epoch is not None and reset_epoch <= now)
        result.append({**item, 'observed_at': observed, 'resets_at': reset,
                       'age_seconds': round(age, 3) if age is not None and age >= 0 else None,
                       'stale_at': _instant(expiry), 'stale': stale})
    return result


def _codex_limit(rows, limit, pool, observed_at):
    if not isinstance(limit, dict):
        return
    limit = limit.get('rate_limit', limit.get('rateLimit', limit))
    if not isinstance(limit, dict):
        return
    for part in ('primary', 'secondary'):
        item = limit.get(f'{part}_window', limit.get(f'{part}Window'))
        if not isinstance(item, dict):
            continue
        used = _number(item.get('used_percent', item.get('usedPercent')), maximum=100)
        if used is None:
            continue
        label = part if pool == 'base' else f'{pool} · {part}'
        duration = item.get('limit_window_seconds', item.get('limitWindowSeconds'))
        rows.append(_row(f'{pool}:{part}', label, 'account' if pool == 'base' else 'unknown',
                         None, used, duration, _reset(item, observed_at), observed_at))


def active(provider, payload, observed_at):
    """Project only quota fields from one successful upstream response."""
    if not isinstance(payload, dict):
        return []
    if provider == 'codex':
        rows = []
        _codex_limit(rows, payload.get('rate_limit', payload.get('rateLimit')), 'base', observed_at)
        _codex_limit(rows, payload.get('code_review_rate_limit', payload.get('codeReviewRateLimit')),
                     'code_review', observed_at)
        additional = payload.get('additional_rate_limits', payload.get('additionalRateLimits'))
        for item in additional[:50] if isinstance(additional, list) else []:
            if not isinstance(item, dict):
                continue
            name = item.get('limit_name', item.get('limitName', item.get('metered_feature')))
            if not isinstance(name, str) or not 0 < len(name) <= 128 or any(ord(c) < 32 for c in name):
                continue
            _codex_limit(rows, item, name, observed_at)
        return rows[:100]
    if provider == 'claude':
        rows = []
        for item in normalize_native('claude', payload)[:100]:
            used = _number(item.get('used_percent'), maximum=1000)
            if used is None:
                continue
            row = _row(item['id'], item['label'], item['scope'], item.get('model_id'),
                       used, item.get('window_seconds'), item.get('resets_at'), observed_at,
                       status=item.get('status'))
            for key in ('model_family', 'model_family_label', 'surface_id', 'surface_label'):
                value = text(item.get(key))
                if value is not None:
                    row[key] = value
            rows.append(row)
        return rows
    return []
