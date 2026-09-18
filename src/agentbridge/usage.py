"""Quota snapshots are distinct from per-run tokens and monetary cost."""
from datetime import datetime,timezone
import json
from pathlib import Path
import time
import urllib.request
import urllib.error
from . import usage_rollouts


def snapshot(account, *, oauth_token=None, allow_network=False):
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
        req=urllib.request.Request('https://api.anthropic.com/api/oauth/usage',headers={
            'Authorization':'Bearer '+oauth_token,'anthropic-beta':'oauth-2025-04-20','Accept':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=10) as response:data=json.load(response)
        except (OSError,ValueError):return {**base,'supported':True,'reason':'provider_unavailable'}
        windows=[{'name':k,'used_percent':v.get('utilization'),'resets_at':v.get('resets_at')}
                 for k,v in data.items() if isinstance(v,dict) and 'utilization' in v]
        return {**base,'supported':True,'source':'claude_oauth','observed_at':datetime.now(timezone.utc).isoformat(),
                'outdated':False,'windows':windows,'reason':None if windows else 'quota_unknown'}
    return {**base,'reason':'account_quota_unsupported'}


def summarize(events):
    """Return observations without adding cumulative totals or overlapping scopes."""
    observations=[{'at':e.at,**e.data} for e in events if e.kind=='usage']
    return {'supported':bool(observations),'observations':observations,
            'reason':None if observations else 'not_reported'}
