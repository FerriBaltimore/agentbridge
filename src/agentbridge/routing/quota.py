"""Project complete, attributable quota windows into routing observations."""

import math
import time

from ..quota_windows import timestamp
from .selector import QuotaObservation


ROUTE_QUOTA_TTL = 60


def _applies_to_model(window, model):
    """Return True, False, or None when the provider scope is unproven."""
    scope = window.get('scope')
    scoped_model = window.get('model_id')
    if scope == 'model':
        return scoped_model == model if isinstance(scoped_model, str) else None
    if scope == 'account':
        return True if scoped_model is None else None
    if isinstance(scoped_model, str):
        return True if scoped_model == model else False
    return None


def route_quota(snapshot, model):
    """Preserve each applicable window; an ambiguous scope keeps quota unknown."""
    windows = snapshot.get('quota_windows', ()) if isinstance(snapshot, dict) else ()
    observations = []
    ambiguous = False
    for window in windows if isinstance(windows, (list, tuple)) else ():
        if not isinstance(window, dict):
            continue
        applies = _applies_to_model(window, model)
        if applies is False:
            continue
        if applies is None:
            ambiguous = True
            continue
        used = window.get('used_percent')
        if (isinstance(used, bool) or not isinstance(used, (int, float))
                or not math.isfinite(used) or used < 0):
            used = None
        elif used > 100:
            used = 100.0
        limit_reached = (True if window.get('limit_reached') is True
                         or window.get('status') == 'rejected' else
                         False if window.get('limit_reached') is False else None)
        observations.append(QuotaObservation(
            used, timestamp(window.get('observed_at')), model_id=model,
            reset_at=timestamp(window.get('resets_at')),
            limit_reached=limit_reached,
            source=window.get('source') or snapshot.get('source'),
            window_id=window.get('id')))
    if ambiguous:
        observations.append(QuotaObservation(None, None, model_id=model))
    return tuple(observations)


def needs_active_refresh(snapshot, model, *, now=None):
    """Refresh missing or stale evidence once per TTL, excluding fresh active reads."""
    now = time.time() if now is None else now
    windows = snapshot.get('quota_windows', ()) if isinstance(snapshot, dict) else ()
    recent_active = []
    for window in windows if isinstance(windows, (list, tuple)) else ():
        if (not isinstance(window, dict)
                or window.get('source') != 'cliproxy_upstream_usage'
                or _applies_to_model(window, model) is False):
            continue
        observed = timestamp(window.get('observed_at'))
        if observed is not None and 0 <= now - observed < ROUTE_QUOTA_TTL:
            recent_active.append(window)
    if recent_active:
        if any((reset := timestamp(window.get('resets_at'))) is not None and reset <= now
               for window in recent_active):
            return True
        return False
    observations = route_quota(snapshot, model)
    if not observations:
        return True
    return any(observed.observed_at is None or observed.used_percent is None
               or observed.observed_at > now or now - observed.observed_at >= ROUTE_QUOTA_TTL
               or observed.reset_at is not None and observed.reset_at <= now
               for observed in observations)
