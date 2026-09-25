"""Verify an automatic account switch against current transactional evidence."""

import json
import time

from ..errors import BridgeError
from .quota import ROUTE_QUOTA_TTL


def _invalid():
    raise BridgeError('invalid_request',
                      'Changing the affinity account requires valid break evidence.')


def _account_config(db, account_id):
    row = db.execute('SELECT config FROM accounts WHERE id=?', (account_id,)).fetchone()
    return json.loads(row['config']) if row else None


def _context_ceiling(db, account_id, model):
    row = db.execute('SELECT data FROM account_observations WHERE account_id=? '
                     'ORDER BY observed_at DESC,id DESC LIMIT 1', (account_id,)).fetchone()
    data = json.loads(row['data']) if row else {}
    models = data.get('model_metadata')
    controls = models.get(model) if isinstance(models, dict) else None
    maximum = controls.get('max_context_window') if isinstance(controls, dict) else None
    return maximum if type(maximum) is int and 0 < maximum <= 10_000_000 else None


def require_affinity_break(db, store, *, affinity, selected, model, provider,
                           context_window, exclusions, event):
    """Accept only a documented reason to leave the saved affinity account."""
    reason = event.get('affinity_break_reason')
    evidence = event.get('affinity_break_evidence')
    if affinity is None or selected == affinity:
        if reason is not None or evidence is not None:
            _invalid()
        return
    if not isinstance(evidence, dict) or evidence.get('account_id') != affinity:
        _invalid()
    if reason == 'quota_exhausted':
        now = time.time()
        observed = evidence['observed_at']
        reset_time = evidence['reset_at']
        invalidated = db.execute("SELECT MAX(updated) FROM account_reset_attempts "
                                 "WHERE account_id=? AND state='done' "
                                 "AND outcome IN ('reset','already_redeemed')",
                                 (affinity,)).fetchone()[0]
        if (not 0 <= now - observed < ROUTE_QUOTA_TTL
                or reset_time is not None and reset_time <= now
                or invalidated is not None and observed < invalidated):
            raise BridgeError('quota_unknown',
                              'The affinity quota changed after route selection.',
                              retryable=True)
        return
    if reason == 'explicit_exclusion':
        from ..accounts import AccountService
        excluded_ids = {AccountService(store).resolve(reference).id
                        for reference in exclusions}
        if affinity in excluded_ids:
            return
    elif reason == 'account_paused':
        if db.execute('SELECT 1 FROM paused_accounts WHERE account_id=?',
                      (affinity,)).fetchone():
            return
    elif reason == 'account_retired':
        if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',
                      (affinity,)).fetchone():
            return
    elif reason == 'model_incompatible':
        config = _account_config(db, affinity)
        if config is not None and model not in config.get('supported_models', []):
            return
    elif reason == 'provider_incompatible':
        config = _account_config(db, affinity)
        if provider is not None and config is not None and config.get('provider') != provider:
            return
    elif reason == 'context_window_incompatible':
        requested = evidence['context_window']
        ceiling = _context_ceiling(db, affinity, model)
        if context_window == requested and ceiling is not None and requested > ceiling:
            return
    _invalid()
