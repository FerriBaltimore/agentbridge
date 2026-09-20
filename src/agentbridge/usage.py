"""Quota snapshots are distinct from per-run tokens and monetary cost."""
from datetime import datetime,timezone
from pathlib import Path
import time
from . import usage_rollouts
from .quota_windows import project


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
        for name in ('primary','secondary'):
            win=(tc.get('rate_limits') or {}).get(name)
            if isinstance(win,dict):windows.append({'name':name,**win})
        stamp=tc.get('at')
        try:age=now-datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
        except (ValueError,AttributeError,TypeError):age=float('inf')
        return {**base,'supported':True,'source':'codex_rollout','observed_at':stamp,'outdated':age>1800,
                'windows':windows,'tokens':(tc.get('info') or {}),'reason':None if windows else 'quota_unknown'}
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
