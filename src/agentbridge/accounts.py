"""Account identity and usage service, independent from process supervision.

The service never stores credential values. Provider probes return bounded,
observable facts only. A missing observation is different from an empty quota.
"""
from datetime import datetime, timezone
import json
import os
import select
import signal
import subprocess
import time

from . import usage
from .errors import BridgeError
from .models import Account, account_name_key, identifier
from .security import base_environment


def _stamp(value=None):
    return datetime.fromtimestamp(value or time.time(), timezone.utc).isoformat(timespec='seconds')


def _text(value, maximum=320):
    return value if isinstance(value, str) and 0 < len(value) <= maximum else None


def _safe_identity(account, provider_account):
    value = provider_account if isinstance(provider_account, dict) else {}
    result = {}
    for key in ('type', 'email', 'name', 'planType', 'plan_type', 'account_id', 'accountId'):
        item = value.get(key)
        if _text(item):
            result[key] = item
    return result


class CodexAppServerProbe:
    """Read Codex account facts without running a model turn."""

    source = 'codex_app_server'

    def __init__(self, account, *, timeout=30):
        self.account = account
        self.timeout = timeout
        self.process = None
        self.next_id = 0

    def _command(self):
        return list(self.account.command or ('codex',)) + ['app-server', '--stdio']

    def _start(self):
        env = base_environment()
        env['CODEX_HOME'] = self.account.home
        for name in (*self.account.env_names, *((self.account.key_env,) if self.account.key_env else ())):
            if name not in os.environ:
                raise BridgeError('credential_unavailable', f'Required environment variable {name} is unavailable.')
            env[name] = os.environ[name]
        try:
            self.process = subprocess.Popen(self._command(), cwd=self.account.home, env=env,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                            start_new_session=True)
        except OSError as error:
            raise BridgeError('provider_unavailable', 'The provider process could not be started.') from error

    def _rpc(self, method, params=None):
        if not self.process:
            self._start()
        self.next_id += 1
        request = {'id': self.next_id, 'method': method, 'params': params or {}}
        try:
            self.process.stdin.write(json.dumps(request, separators=(',', ':')) + '\n')
            self.process.stdin.flush()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                ready, _, _ = select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    break
                line = self.process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if message.get('id') != request['id']:
                    continue
                if isinstance(message.get('error'), dict):
                    raise BridgeError('provider_failed', 'The provider rejected the account query.')
                result = message.get('result')
                if not isinstance(result, dict):
                    raise BridgeError('provider_protocol_error', 'The provider returned an invalid account response.')
                return result
        except (BrokenPipeError, OSError):
            raise BridgeError('provider_failed', 'The provider connection ended during the account query.') from None
        raise BridgeError('provider_timeout', 'The provider did not answer the account query in time.')

    def read(self, *, include_usage=False):
        try:
            self._rpc('initialize', {'clientInfo': {'name': 'agentbridge', 'title': 'AgentBridge', 'version': '0.1'}})
            self._notify('initialized', {})
            result = self._rpc('account/read', {'refreshToken': False})
            provider_account = result.get('account')
            identity = _safe_identity(self.account, provider_account)
            if provider_account is None:
                status = 'authentication_required' if result.get('requiresOpenaiAuth') else 'unauthenticated'
            elif isinstance(provider_account, dict):
                status = 'authenticated'
            else:
                status = 'provider_protocol_error'
            data = {'status': status, 'identity': identity,
                    'requires_openai_auth': bool(result.get('requiresOpenaiAuth'))}
            if include_usage and status == 'authenticated':
                try:
                    data['quota'] = self._rpc('account/rateLimits/read')
                except BridgeError as error:
                    data['quota_reason'] = error.code
                try:
                    data['account_usage'] = self._rpc('account/usage/read')
                except BridgeError as error:
                    data['account_usage_reason'] = error.code
            return data
        finally:
            self.close()

    def _notify(self, method, params):
        if not self.process:
            return
        self.process.stdin.write(json.dumps({'method': method, 'params': params}, separators=(',', ':')) + '\n')
        self.process.stdin.flush()

    def close(self):
        process, self.process = self.process, None
        if not process:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass


class AccountService:
    """Account registry, identity health and provider usage observations."""

    def __init__(self, store):
        self.store = store

    def register(self, account):
        self.store.account(account)
        return self.get(account.id)

    def get(self, account_id):
        account_id = identifier(account_id)
        row = self.store.get('accounts', account_id)
        return Account(**json.loads(row['config']))

    def resolve(self, reference):
        """Resolve a human-facing unique name or an internal account ID."""
        if isinstance(reference, str):
            wanted_name = account_name_key(reference)
            for account in self.list():
                if account.name and account_name_key(account.name) == wanted_name:
                    return account
        try:
            return self.get(reference)
        except BridgeError as error:
            if error.code in ('invalid_id', 'not_found'):
                raise BridgeError('account_not_found', 'No account matches that name.') from None
            raise

    def list(self):
        return [Account(**json.loads(row['config'])) for row in self.store.list('accounts')]

    def status(self, account_id, *, refresh=False):
        account = self.get(account_id)
        observation = self.store.latest_account_observation(account.id)
        if refresh:
            self._probe_account(account, include_usage=False)
            observation = self.store.latest_account_observation(account.id)
        configured = {'name': account.name, 'email': account.email, 'engine': account.engine,
                      'credential_ref': bool(account.env_names or account.key_env or account.home)}
        if not observation:
            return {'account_id': account.id, 'configured': configured,
                    'authentication': {'status': 'not_observed', 'source': None, 'observed_at': None},
                    'identity': {}, 'reason': 'no_observation'}
        return {'account_id': account.id, 'configured': configured,
                'authentication': {'status': observation['status'], 'source': observation['source'],
                                   'observed_at': _stamp(observation['observed_at'])},
                **observation['data']}

    def usage(self, account_id, *, refresh=False):
        account = self.get(account_id)
        if refresh:
            observation = self._probe_account(account, include_usage=True)
            data = observation['data'].get('usage')
            if data is not None:
                return data
        cached = self.store.latest_usage_observation(account.id)
        if cached:
            return {'account_id': account.id, 'observed_at': _stamp(cached['observed_at']),
                    'source': cached['source'], 'scope': cached['scope'],
                    'stale': cached['stale'], **cached['data']}
        if account.engine == 'codex':
            data = usage.snapshot(account)
            self.store.usage_observation(account.id, data.get('source', 'codex_rollout'), 'account', data,
                                         stale=bool(data.get('outdated', True)))
            return {'account_id': account.id, 'observed_at': data.get('observed_at'),
                    'source': data.get('source', 'codex_rollout'), 'scope': 'account',
                    'stale': bool(data.get('outdated', True)), **data}
        return {'account_id': account.id, 'engine': account.engine, 'supported': False,
                'source': None, 'scope': 'account', 'stale': True,
                'reason': 'account_usage_unsupported'}

    def history(self, account_id, *, limit=100):
        self.get(account_id)
        return self.store.usage_history(account_id, limit=limit)

    def _probe_account(self, account, *, include_usage):
        if account.engine != 'codex':
            data = {'status': 'unsupported', 'identity': {}, 'reason': 'provider_probe_unsupported'}
            self.store.account_observation(account.id, 'agentbridge', data['status'], data)
            return {'data': data, 'status': data['status']}
        try:
            result = CodexAppServerProbe(account).read(include_usage=include_usage)
        except BridgeError as error:
            result = {'status': error.code, 'identity': {}, 'reason': error.code}
        self.store.account_observation(account.id, CodexAppServerProbe.source, result['status'], result)
        if include_usage and (result.get('quota') is not None or result.get('account_usage') is not None):
            usage_data = {'supported': True, 'source': 'codex_app_server', 'quota': result.get('quota'),
                          'account_usage': result.get('account_usage')}
            self.store.usage_observation(account.id, 'codex_app_server', 'account', usage_data, stale=False)
            result['usage'] = {'account_id': account.id, 'observed_at': _stamp(), 'source': 'codex_app_server',
                               'scope': 'account', 'stale': False, **usage_data}
        return {'data': result, 'status': result['status']}
