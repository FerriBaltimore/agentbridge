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
from uuid import uuid4

from . import usage
from .errors import BridgeError
from .grantbridge import GrantBridgeClient
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

    def login(self, *, engine, name, email=None, grantbridge_root=None, data_dir=None,
              mode='browser', browser='same_host', timeout=600, poll_interval=1.0,
              on_attempt=None, client=None):
        """Authenticate or refresh a native account through GrantBridge.

        GrantBridge owns the browser flow, provider credentials and native profile.
        AgentBridge only receives a safe identity projection and the provider home
        path after the fresh authorization reaches ``authorized``.
        """
        existing = next((account for account in self.list()
                         if account.name and account_name_key(account.name) == account_name_key(name)), None)
        if existing and existing.engine != engine:
            raise BridgeError('account_engine_mismatch', 'The existing account name belongs to a different engine.')
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 86400:
            raise BridgeError('invalid_timeout', 'Login timeout must be in [1, 86400] seconds.')
        if not isinstance(poll_interval, (int, float)) or not 0.05 <= poll_interval <= 60:
            raise BridgeError('invalid_poll_interval', 'Login poll interval must be in [0.05, 60] seconds.')
        account_id = existing.id if existing else uuid4().hex
        client = client or GrantBridgeClient(grantbridge_root, data_dir=data_dir, timeout=min(30, timeout))
        attempt = None
        started = time.monotonic()
        try:
            attempt = client.start(owner=account_id, engine=engine, mode=mode, browser=browser,
                                   request_key=account_id)
            if on_attempt:
                on_attempt(attempt)
            while attempt.get('status') not in {'authorized', 'failed', 'cancelled', 'expired', 'interrupted', 'revoked', 'replaced'}:
                if time.monotonic() - started >= timeout:
                    try:
                        client.cancel(attempt['id'], account_id)
                    except BridgeError:
                        pass
                    raise BridgeError('login_timeout', 'The authentication attempt timed out.')
                time.sleep(poll_interval)
                attempt = client.get(attempt['id'], account_id)
                if on_attempt:
                    on_attempt(attempt)
            if attempt.get('status') != 'authorized':
                detail = attempt.get('error') or {}
                code = detail.get('code', 'login_failed')
                messages = {
                    'provider_unavailable': 'The provider process could not be started.',
                    'activation_unsupported': 'This provider does not expose a native home to AgentBridge yet.',
                    'native_reauthorization_required': 'The provider requires a new login.',
                    'login_timeout': 'The authentication attempt timed out.',
                }
                raise BridgeError(code, messages.get(code, 'The provider did not complete authentication.'))
            activation = client.activate(attempt['id'], account_id)
            home = activation.get('home') if isinstance(activation, dict) else None
            if not home:
                raise BridgeError('activation_invalid', 'GrantBridge did not return a native account home.')
            identity = activation.get('identity') if isinstance(activation, dict) else {}
            observed_email = identity.get('email') if isinstance(identity, dict) else None
            promoted = Account(account_id, engine, home=home,
                               name=existing.name if existing else name,
                               email=observed_email or email or (existing.email if existing else None),
                               env_names=existing.env_names if existing else (),
                               key_env=existing.key_env if existing else None,
                               command=existing.command if existing else ())
            account = self.store.replace_account_home(promoted) if existing else self.register(promoted)
            account = self.get(account_id)
            self.store.account_observation(account.id, 'grantbridge', 'authenticated',
                                           {'identity': identity or {}, 'source': 'grantbridge_native_login'})
            return {'account': account.to_dict(), 'attempt': attempt,
                    'identity': identity or {}, 'home': home}
        finally:
            client.close()

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
