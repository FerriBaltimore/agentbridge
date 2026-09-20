"""Codex pool and spend-control facts, separate from usage reset credits.

Provider decimal amounts have no declared currency in the native schema.
Preserve their strings and report an unknown unit instead of inventing dollars.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import time


def _text(value):
    return value if isinstance(value, str) and 0 < len(value) <= 512 and all(ord(c) >= 32 and ord(c) != 127 for c in value) else None


def _boolean(value):
    return value if type(value) is bool else None


def _amount(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value.strip() != value:
        return None
    try:
        parsed = Decimal(value)
        return value if parsed.is_finite() and parsed >= 0 else None
    except InvalidOperation:
        return None


def _individual(raw, now):
    if not isinstance(raw, dict):
        return None
    percent = raw.get('remainingPercent')
    if (isinstance(percent, bool) or not isinstance(percent, (int, float))
            or not 0 <= percent <= 100):
        percent = None
    reset = raw.get('resetsAt')
    reset_at = None
    if type(reset) is int:
        try:
            reset_at = datetime.fromtimestamp(reset, timezone.utc).isoformat(timespec='seconds')
        except (ValueError, OverflowError, OSError):
            pass
    return {'limit': _amount(raw.get('limit')), 'used': _amount(raw.get('used')), 'unit': None,
            'remaining_percent': percent, 'used_percent': 100 - percent if percent is not None else None,
            'resets_at': reset_at,
            'reset_after_seconds': max(0, math.ceil(reset - now)) if reset_at else None,
            'reset_due': reset <= now if reset_at else None,
            'limit_reached': percent == 0 if percent is not None else None}


def codex_spend(raw, now=None):
    """Normalize the multi-bucket rateLimits response, including windowless pools.

Null spendControlReached is unknown, not false. Credit balances are account
spend facts and never the number of earned quota-reset redemptions.
"""
    if not isinstance(raw, dict):
        return []
    now = time.time() if now is None else now
    buckets = raw.get('rateLimitsByLimitId')
    buckets = dict(buckets) if isinstance(buckets, dict) else {}
    legacy = raw.get('rateLimits')
    if isinstance(legacy, dict):
        buckets.setdefault(_text(legacy.get('limitId')) or 'codex', legacy)
    if not buckets and any(key in raw for key in ('primary', 'secondary', 'spendControlReached', 'individualLimit')):
        buckets[_text(raw.get('limitId')) or _text(raw.get('limit_id')) or 'codex'] = raw
    rows = []
    for key, bucket in list(buckets.items())[:100]:
        if not _text(key) or not isinstance(bucket, dict):
            continue
        raw_credits = bucket.get('credits')
        credits = None
        if isinstance(raw_credits, dict):
            credits = {'has_credits': _boolean(raw_credits.get('hasCredits')),
                       'unlimited': _boolean(raw_credits.get('unlimited')),
                       'balance': _amount(raw_credits.get('balance')), 'unit': None}
        rows.append({'pool_id': key, 'label': _text(bucket.get('limitName')),
                     'plan': _text(bucket.get('planType')), 'credits': credits,
                     'spend_control_reached': _boolean(bucket.get('spendControlReached')),
                     'rate_limit_reached_type': _text(bucket.get('rateLimitReachedType')),
                     'individual_limit': _individual(bucket.get('individualLimit'), now)})
    return rows
