"""Quota snapshots are distinct from per-run tokens and monetary cost."""
from datetime import datetime,timezone
from pathlib import Path
import time
from . import usage_rollouts
from .quota_windows import project, timestamp


def snapshot(account, *, oauth_token=None, allow_network=False):
    return project(account.engine, _snapshot(account, oauth_token=oauth_token, allow_network=allow_network))


def _snapshot(account, *, oauth_token=None, allow_network=False):
    now=time.time()
    base={'account_id':account.id,'engine':account.engine,'scope':'account','observed_at':None,
          'supported':False,'windows':[],'outdated':True}
    if account.engine=='codex':
        tc=usage_rollouts.newest_token_count([Path(account.home)])
        if not tc:return {**base,'supported':True,'reason':'no_local_observation'}
        windows=[]
        limits = tc.get('rate_limits')
        limits = limits if isinstance(limits, dict) else {}
        for name in ('primary','secondary'):
            win=limits.get(name)
            if isinstance(win,dict):windows.append({'name':name,**win})
        stamp=tc.get('at')
        observed = timestamp(stamp)
        age = now - observed if observed is not None else None
        return {**base,'supported':True,'source':'codex_rollout','observed_at':stamp,
                'outdated':age is None or age < 0 or age >= 1800,
                'windows':windows,'tokens':tc.get('info') if isinstance(tc.get('info'), dict) else {},
                'reason':None if windows else 'quota_unknown'}
    if account.engine=='claude':
        if not allow_network:return {**base,'supported':True,'reason':'network_not_requested'}
        if not oauth_token:return {**base,'supported':True,'reason':'oauth_token_required'}
        from .claude_account import quota_request
        from .errors import BridgeError
        try:
            data = quota_request(oauth_token)
        except BridgeError as error:
            return {**base, 'supported': True, 'reason': error.code}
        return {**base, **data, 'observed_at': datetime.now(timezone.utc).isoformat(), 'outdated': False}
    return {**base,'reason':'account_quota_unsupported'}


def summarize(events):
    """Return observations without adding cumulative totals or overlapping scopes."""
    observations=[{'at':e.at,**e.data} for e in events if e.kind=='usage']
    return {'supported':bool(observations),'observations':observations,
            'reason':None if observations else 'not_reported'}
