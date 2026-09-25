"""Model-first route discovery from safe local proxy observations."""

import os
from threading import Lock
import time

from ..errors import BridgeError
from ..proxy import ManagementClient, ProxyRoute
from ..proxy.quota import project as project_quota
from ..quota_windows import timestamp
from .quota import needs_active_refresh, route_quota
from .selector import RouteCandidate, select_route


OBSERVATION_TTL = 30
MAX_ACTIVE_QUOTA_REFRESHES = 4
ACTIVE_REFRESH_FAILURE_TTL = 30
_ACTIVE_REFRESH_FAILURES = {}
_ACTIVE_REFRESH_FAILURES_LOCK = Lock()


class RoutingService:
    def __init__(self, store, accounts, managed_proxy=None):
        self.store = store
        self.accounts = accounts
        self.managed_proxy = managed_proxy

    def _ensure_managed(self, account):
        if self.managed_proxy and self.managed_proxy.is_managed(account.to_dict(), account.id):
            self.managed_proxy.ensure(account.id, account.proxy_base_url)

    def _record_failure(self, account, code):
        self.store.account_observation(account.id, 'cliproxy_management', 'unknown',
                                       {'verified': False, 'reason': code})
        self.store.usage_observation(account.id, 'cliproxy_management', 'account',
                                     {'supported': False, 'quota_windows': [],
                                      'reason': code}, stale=True)

    def observe(self, account, *, include_catalog=True):
        """Persist only an allowlisted observation; a failed read is unknown."""
        if not account.management_key_env:
            return None
        route = ProxyRoute(account.id, account.proxy_base_url, account.key_env)
        try:
            self._ensure_managed(account)
            if not self.store.proxy_login_origin(account):
                raise BridgeError('authentication_required', 'This proxy account has no completed GrantBridge login.')
            management = ManagementClient(route, account.management_key_env)
            data = management.observe()
            if (data.get('provider') != account.provider or data.get('status') != 'active'
                    or data.get('disabled') is not False or data.get('unavailable') is not False):
                raise BridgeError('proxy_binding_unverified', 'The local proxy account is not usable.')
            self.store.bind_proxy_account(account.id, data['binding_fingerprint'],
                                          data['identity_fingerprint'])
            data = {key: value for key, value in data.items()
                    if key not in {'binding_fingerprint', 'identity_fingerprint', 'email'}}
            data['binding_verified'] = True
            if include_catalog:
                catalog = management.catalog()
                observed_ids = {item['id'] for item in data['models']}
                data['model_metadata'] = {key: value for key, value in catalog.items()
                                          if key in observed_ids}
                data['model_metadata_source'] = ('cliproxy_client_models'
                                                 if data['model_metadata'] else None)
        except BridgeError as error:
            self._record_failure(account, error.code)
            return None
        self.store.account_observation(account.id, 'cliproxy_management',
                                       data.get('status') or 'unknown', data)
        snapshot = self._usage_snapshot(account.id, data)
        self.store.usage_observation(account.id, 'cliproxy_management', 'account',
                                     {key: snapshot[key] for key in ('supported', 'quota_windows', 'reason')},
                                     stale=snapshot['stale'])
        return data

    def observation(self, account, *, refresh=False, now=None, include_catalog=True):
        if not account.proxy_base_url:
            return None
        try:
            self._ensure_managed(account)
        except BridgeError as error:
            self._record_failure(account, error.code)
            return self.store.latest_account_observation(account.id)
        if not self.store.proxy_login_origin(account):
            if refresh:
                self.observe(account)
            return None
        now = time.time() if now is None else now
        saved = self.store.latest_account_observation(account.id)
        fresh = (saved is not None and saved['source'] == 'cliproxy_management'
                 and 0 <= now - saved['observed_at'] < OBSERVATION_TTL)
        if refresh or not fresh:
            self.observe(account, include_catalog=include_catalog)
            saved = self.store.latest_account_observation(account.id)
        return saved if saved and saved['source'] == 'cliproxy_management' else None

    @staticmethod
    def _verified(account, saved):
        if not saved:
            return False
        data = saved['data']
        return (data.get('account_id') == account.id
                and data.get('provider') == account.provider
                and data.get('status') == 'active'
                and data.get('disabled') is False
                and data.get('unavailable') is False
                and data.get('binding_verified') is True
                and isinstance(data.get('models'), list))

    def candidates(self, model, *, provider=None, refresh=False, excluded_account_refs=(),
                   affinity_account_id=None, fast_affinity=False):
        excluded = {self.accounts.resolve(reference).id for reference in excluded_account_refs}
        paused = self.store.paused_account_ids()
        pending = self.store.pending_reset_account_ids()
        accounts = [account for account in self.accounts.list()
                    if account.proxy_base_url and model in account.supported_models
                    and (provider is None or account.provider == provider)
                    and account.id not in excluded and account.id not in paused
                    and account.id not in pending]
        if affinity_account_id is not None:
            accounts.sort(key=lambda account: account.id != affinity_account_id)
        loads = self.store.route_load([account.id for account in accounts])
        result = []
        active_refreshes = 0
        for account in accounts:
            saved = self.observation(account, refresh=refresh)
            verified = self._verified(account, saved) and bool(os.environ.get(account.key_env))
            models = {item.get('id'): item for item in saved['data']['models']
                      if isinstance(item, dict)} if verified else {}
            declared_and_seen = tuple(item for item in account.supported_models if item in models)
            item = models.get(model) if verified else None
            quotas = ()
            if verified and item is not None:
                quota_snapshot = self._quota_snapshot(account, saved)
                if (refresh and account.provider in {'codex', 'claude'}
                        and active_refreshes < MAX_ACTIVE_QUOTA_REFRESHES
                        and needs_active_refresh(quota_snapshot, model)
                        and not self._recent_active_refresh_failure(account)):
                    quota_snapshot = self._quota_snapshot(account, saved, refresh_active=True)
                    active_refreshes += 1
                quotas = route_quota(quota_snapshot, model)
            load = loads[account.id]
            result.append(RouteCandidate(
                account_id=account.id, models=declared_and_seen if verified else account.supported_models,
                quota=quotas, health='healthy' if verified else 'unhealthy',
                cooldown_until=timestamp(item.get('cooldown_until')) if item else None,
                in_flight=load['in_flight'], assigned_turns=load['assigned_turns']))
            if fast_affinity and account.id == affinity_account_id:
                try:
                    select_route(model, (result[-1],), affinity_account_id=affinity_account_id)
                except BridgeError as error:
                    if error.code not in {'quota_exhausted', 'model_unavailable'}:
                        break
                else:
                    break
        return result

    def _active_refresh_key(self, account):
        return str(getattr(self.store, 'path', id(self.store))), account.id

    def _recent_active_refresh_failure(self, account):
        with _ACTIVE_REFRESH_FAILURES_LOCK:
            failed_at = _ACTIVE_REFRESH_FAILURES.get(self._active_refresh_key(account))
        return failed_at is not None and time.monotonic() - failed_at < ACTIVE_REFRESH_FAILURE_TTL

    def _record_active_refresh_failure(self, account):
        now = time.monotonic()
        with _ACTIVE_REFRESH_FAILURES_LOCK:
            for key, failed_at in list(_ACTIVE_REFRESH_FAILURES.items()):
                if now - failed_at >= ACTIVE_REFRESH_FAILURE_TTL:
                    del _ACTIVE_REFRESH_FAILURES[key]
            _ACTIVE_REFRESH_FAILURES[self._active_refresh_key(account)] = now

    def _clear_active_refresh_failure(self, account):
        with _ACTIVE_REFRESH_FAILURES_LOCK:
            _ACTIVE_REFRESH_FAILURES.pop(self._active_refresh_key(account), None)

    def context_ceiling(self, account, model, *, refresh=False):
        """Return the verified proxy catalog ceiling, or unknown."""
        saved = self.observation(account, refresh=refresh)
        if not self._verified(account, saved):
            return None
        metadata = saved['data'].get('model_metadata')
        controls = metadata.get(model) if isinstance(metadata, dict) else None
        maximum = controls.get('max_context_window') if isinstance(controls, dict) else None
        return maximum if type(maximum) is int and 0 < maximum <= 10_000_000 else None

    def select(self, model, *, provider=None, refresh=True, excluded_account_refs=(),
               context_window=None, affinity_account_id=None):
        from .admission import normalized_exclusions

        excluded = normalized_exclusions(excluded_account_refs)
        if (context_window is not None and
                (type(context_window) is not int or not 0 < context_window <= 10_000_000)):
            raise BridgeError('invalid_context_window', 'context_window must be a positive token count.')
        candidates = self.candidates(model, provider=provider, refresh=refresh,
                                     excluded_account_refs=excluded,
                                     affinity_account_id=affinity_account_id,
                                     fast_affinity=context_window is None)
        if context_window is not None:
            declared_candidates = candidates
            eligible = []
            for item in candidates:
                ceiling = self.context_ceiling(self.accounts.resolve(item.account_id), model)
                if (item.account_id == affinity_account_id and ceiling is None):
                    raise BridgeError('context_window_unavailable',
                                      'The affinity account has no verified context window ceiling.')
                if ceiling is not None and context_window <= ceiling:
                    eligible.append(item)
            candidates = eligible
            if not candidates and declared_candidates:
                raise BridgeError('context_window_unavailable',
                                  'No verified account supports the requested context window.')
        return select_route(model, candidates, affinity_account_id=affinity_account_id)

    def models(self, *, account_ref=None, provider=None, refresh=False):
        """Return a configured catalog, marking proxy observations separately."""
        pending = self.store.pending_reset_account_ids()
        accounts = [account for account in self.accounts.list() if account.proxy_base_url
                    and account.id not in self.store.paused_account_ids()
                    and account.id not in pending]
        if account_ref is not None:
            selected = self.accounts.resolve(account_ref)
            accounts = [account for account in accounts if account.id == selected.id]
        if provider is not None:
            accounts = [account for account in accounts if account.provider == provider]
        grouped = {}
        for account in accounts:
            saved = self.observation(account, refresh=refresh, include_catalog=True)
            seen = ({item.get('id') for item in saved['data']['models'] if isinstance(item, dict)}
                    if self._verified(account, saved) else set())
            metadata = saved['data'].get('model_metadata', {}) if seen else {}
            for model in account.supported_models:
                row = grouped.setdefault(model, {'id': model, 'display_name': model,
                    'availability': 'configured_unverified', 'source': 'account_configuration',
                    'candidate_account_refs': [], 'observed_account_refs': [],
                    'providers': [], 'reasoning_efforts': [], 'context_windows': [],
                    'default_context_window': None, 'max_context_window': None,
                    'input_modalities': [], 'account_capabilities': []})
                reference = self.accounts.reference(account.id)
                row['candidate_account_refs'].append(reference)
                controls = metadata.get(model, {}) if model in seen else {}
                row['account_capabilities'].append({
                    'account_ref': reference, 'provider': account.provider,
                    'observed': model in seen,
                    'reasoning_efforts': controls.get('reasoning_efforts', []),
                    'default_reasoning_effort': controls.get('default_reasoning_effort'),
                    'context_windows': controls.get('context_windows', []),
                    'default_context_window': controls.get('default_context_window'),
                    'max_context_window': controls.get('max_context_window'),
                    'input_modalities': controls.get('input_modalities', []),
                    'metadata_source': saved['data'].get('model_metadata_source')
                    if controls else None,
                })
                if model in seen:
                    row['observed_account_refs'].append(reference)
                    row['availability'] = 'proxy_observed'
                if account.provider not in row['providers']:
                    row['providers'].append(account.provider)
        for row in grouped.values():
            observed = [item for item in row['account_capabilities'] if item['observed']]
            if observed:
                row['reasoning_efforts'] = [value for value in observed[0]['reasoning_efforts']
                                            if all(value in item['reasoning_efforts'] for item in observed)]
                maxima = [item['max_context_window'] for item in observed]
                if all(type(value) is int and value > 0 for value in maxima):
                    ceiling = min(maxima)
                    row['max_context_window'] = ceiling
                    row['context_windows'] = sorted({value for item in observed
                        for value in item['context_windows']
                        if type(value) is int and 0 < value <= ceiling})
                defaults = [item['default_context_window'] for item in observed]
                if (all(type(value) is int and value > 0 for value in defaults)
                        and len(set(defaults)) == 1):
                    row['default_context_window'] = defaults[0]
                row['input_modalities'] = [value for value in observed[0]['input_modalities']
                                           if all(value in item['input_modalities'] for item in observed)]
        items = [grouped[key] for key in sorted(grouped)]
        return {'source': 'agentbridge_routing', 'stale': any(
            item['availability'] != 'proxy_observed' for item in items), 'models': items}

    def usage(self, account, *, refresh=False):
        saved = self.observation(account, refresh=refresh)
        return self._quota_snapshot(account, saved, refresh_active=refresh)

    def _quota_snapshot(self, account, saved, *, refresh_active=False):
        """Share one combined passive/active window view with routing and usage."""
        verified = self._verified(account, saved)
        snapshot = self._usage_snapshot(account.id, saved['data'] if verified else None,
                                        failure=saved['data'].get('reason') if saved else None)
        if verified:
            previous = self.store.latest_usage_observation(
                account.id, source='cliproxy_upstream_usage')
            if previous:
                prior = self._usage_snapshot(account.id, {
                    **previous['data'], 'source': previous['source']})
                snapshot = self._newer_usage(snapshot, prior)
        if not refresh_active or not verified or account.provider not in {'codex', 'claude'}:
            return self._after_reset(account.id, snapshot)
        binding = self.store.proxy_binding(account.id)
        if not binding:
            return self._after_reset(account.id, {**snapshot, 'refresh_reason': 'proxy_binding_unverified'})
        route = ProxyRoute(account.id, account.proxy_base_url, account.key_env)
        try:
            active = ManagementClient(route, account.management_key_env, timeout=8).fetch_quota(
                binding['binding_fingerprint'])
        except BridgeError as error:
            self._record_active_refresh_failure(account)
            return self._after_reset(account.id, {**snapshot, 'refresh_reason': error.code})
        self._clear_active_refresh_failure(account)
        result = self._usage_snapshot(account.id, active)
        self.store.usage_observation(account.id, result['source'], 'account',
            {key: result[key] for key in ('supported', 'quota_windows', 'reason')},
            stale=result['stale'])
        return self._after_reset(account.id, self._newer_usage(snapshot, result))

    def _after_reset(self, account_id, snapshot):
        """Do not publish quota percentages observed before a known redemption."""
        invalidated_at = self.store.reset_invalidation_at(account_id)
        if invalidated_at is None:
            return snapshot
        windows = []
        invalidated = False
        for item in snapshot['quota_windows']:
            observed_at = timestamp(item.get('observed_at'))
            if observed_at is None or observed_at < invalidated_at:
                windows.append({**item, 'used_percent': None, 'remaining_percent': None,
                                'stale': True, 'invalidation_reason': 'reset_quota_refresh_required'})
                invalidated = True
            else:
                windows.append(item)
        if not invalidated:
            return snapshot
        fresh = any(not item['stale'] and item.get('used_percent') is not None for item in windows)
        return {**snapshot, 'quota_windows': windows, 'supported': fresh, 'stale': not fresh,
                'reason': None if fresh else 'reset_quota_refresh_required'}

    @staticmethod
    def _newer_usage(first, second):
        def identity(item):
            # Both upstream and passive headers describe the same account
            # period when they report the same explicit duration. Opaque
            # scoped pools stay distinct unless their provider IDs coincide.
            identifier = item.get('id', '').removeprefix('account:')
            if identifier.startswith('base:'):
                return ('base_pool', identifier)
            if item.get('scope') == 'account' and item.get('window_seconds'):
                return ('account_period', item['window_seconds'])
            return ('pool', item.get('scope'), item.get('model_id'),
                    identifier)

        def priority(item):
            observed = timestamp(item.get('observed_at'))
            return (not item.get('stale'), observed if observed is not None else -1)

        chosen = {}
        for snapshot in (first, second):
            for item in snapshot['quota_windows']:
                candidate = {**item, 'source': item.get('source') or snapshot['source']}
                key = identity(candidate)
                if key not in chosen or priority(candidate) > priority(chosen[key]):
                    chosen[key] = candidate
        if not chosen:
            return first
        sources = {item['source'] for item in chosen.values()}
        source = sources.pop() if len(sources) == 1 else 'cliproxy_combined_usage'
        return RoutingService._usage_snapshot(first['account_id'], {
            'source': source, 'quota_windows': list(chosen.values())})

    @staticmethod
    def _usage_snapshot(account_id, data, *, failure=None):
        if isinstance(data, dict) and 'quota_windows' in data:
            windows = project_quota(data['quota_windows'])
        else:
            rows = data.get('models', []) if isinstance(data, dict) else []
            windows = project_quota([{'id': item['id'], 'label': item['id'],
                'scope': 'model', 'model_id': item['id'], 'used_percent': item['used_percent'],
                'remaining_percent': max(0, 100 - item['used_percent']),
                'window_seconds': None, 'resets_at': None,
                'observed_at': item.get('quota_observed_at')}
                for item in rows if isinstance(item, dict) and item.get('used_percent') is not None])
        fresh = any(not item['stale'] and item.get('used_percent') is not None for item in windows)
        reason = None if fresh else (failure or (
            'upstream_quota_stale' if windows else 'upstream_quota_unavailable'))
        return {'account_id': account_id, 'source': data.get('source', 'cliproxy_management')
                if isinstance(data, dict) else 'cliproxy_management' if failure else None,
                'scope': 'account', 'supported': any(item.get('used_percent') is not None for item in windows),
                'stale': not fresh, 'quota_windows': windows, 'reason': reason}
