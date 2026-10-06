"""Owned browser transport and child lifetime for provider login, without durable cookies."""

import json
import threading

from .errors import BridgeError
from .proxy.credential_barrier import require_account


class AuthBrowserChannel:
    def _init_browser_channel(self):
        self._grantbridge_clients = {}
        self._browser_attempts = {}
        self._browser_lock = threading.RLock()
        self._browser_closed = False

    def _client(self, connection, root=None, data_dir=None):
        from .authentication import GrantBridgeClient

        with self._browser_lock:
            if self._browser_closed:
                raise BridgeError('authentication_interrupted', 'The login service has closed.')
            key = json.dumps(connection or {'root': root, 'data_dir': data_dir}, sort_keys=True)
            cached = self._grantbridge_clients.get(key)
            if cached is not None and getattr(cached, 'process', None) is not None:
                if not cached.alive:
                    self._lost_browser_client(cached)
            if key not in self._grantbridge_clients:
                self._grantbridge_clients[key] = (GrantBridgeClient(**connection) if connection
                                                 else GrantBridgeClient(root, data_dir=data_dir))
            return self._grantbridge_clients[key]

    def _browser_recovery(self, row):
        with self._browser_lock:
            active = self._browser_attempts.get(row['id'])
            if active and not getattr(active[0], 'alive', not getattr(active[0], 'closed', False)):
                self._lost_browser_client(active[0])
        if (row['data'].get('browserTransport') == 'rpc'
                and row['id'] not in self._browser_attempts
                and row['status'] in {'starting', 'awaiting_user', 'exchanging'}):
            return self.store.update_auth_attempt(
                row['id'], row['owner'], status='interrupted',
                data={**row['data'], 'error': {'code': 'authentication_outcome_unknown'}})
        return row

    def browser(self, attempt_id, *, owner_ref, action='view', after_sequence=0,
                input=None, asset=None):
        row = self._owned(attempt_id, owner_ref, None)
        with self.store.connect() as db:
            require_account(db, row['account_id'], login=True)
        if row['mode'] != 'hosted' or row['data'].get('browserTransport') != 'rpc':
            raise BridgeError('unsupported_operation', 'This login has no embedded browser.')
        if not isinstance(action, str) or action not in {'view', 'input', 'asset'}:
            raise BridgeError('invalid_params', 'Unknown browser action.')
        if type(after_sequence) is not int or not 0 <= after_sequence <= 2**53 - 1:
            raise BridgeError('invalid_params', 'Invalid frame sequence.')
        if action == 'asset' and (not isinstance(asset, str)
                                  or asset not in {'browser.html', 'browser.js', 'browser.css'}):
            raise BridgeError('invalid_params', 'Unknown browser asset.')
        if action == 'input' and (not isinstance(input, dict)
                                  or len(json.dumps(input)) > 16384):
            raise BridgeError('invalid_params', 'Invalid browser input.')
        row = self._browser_recovery(row)
        terminal = row['status'] not in {'starting', 'awaiting_user', 'exchanging'}
        if action != 'asset' and terminal:
            if action == 'input':
                raise BridgeError('authentication_attempt_not_ready', 'This login has ended.')
            # Placeholder for an ended login; a live `view` relays GrantBridge's own
            # viewport (width, height and scale) untouched.
            return {'status': row['status'], 'done': True, 'ready': False,
                    'sequence': 0, 'origin': '',
                    'viewport': {'width': 390, 'height': 760, 'scale': 1},
                    'expires_at': row['data'].get('expiresAt')}
        if row['id'] not in self._browser_attempts:
            raise BridgeError('authentication_interrupted', 'The browser session has ended.')
        _, connection = self._route(row)
        client = self._client(connection)
        try:
            result = client.browser(
                row['grantbridge_id'], row['owner'], action=action,
                after_sequence=after_sequence, input=input, asset=asset)
        except BridgeError as error:
            if error.code not in {'grantbridge_failed', 'grantbridge_timeout'}:
                raise
            self._lost_browser_client(client)
            raise BridgeError('authentication_outcome_unknown',
                              'The browser connection ended. Start a new login.',
                              outcome='unknown', retryable=False) from error
        if action == 'view' and result.get('done'):
            # The browser channel never authorizes an account: status/check owns that transition.
            self.status(attempt_id, owner_ref=owner_ref)
        return result

    def _lost_browser_client(self, client):
        with self._browser_lock:
            lost = [(attempt_id, owner) for attempt_id, (current, owner)
                    in self._browser_attempts.items() if current is client]
            for key, current in list(self._grantbridge_clients.items()):
                if current is client:
                    self._grantbridge_clients.pop(key)
            for attempt_id, _ in lost:
                self._browser_attempts.pop(attempt_id)
        try:
            client.close()
        except Exception:
            # Loss is already known; cleanup errors must not prevent durable interruption.
            pass
        finally:
            for attempt_id, owner in lost:
                self._browser_recovery(self.store.get_auth_attempt(attempt_id, owner))

    def close(self):
        with self._browser_lock:
            if self._browser_closed:
                return
            self._browser_closed = True
            clients = list(self._grantbridge_clients.values())
            attempts = list(self._browser_attempts)
        errors = []
        try:
            for client in clients:
                try:
                    self._lost_browser_client(client)
                except Exception as error:
                    errors.append(error)
        finally:
            # Reconcile even sessions whose close or individual status update failed.
            with self.store.connect() as db:
                for attempt_id in attempts:
                    db.execute("UPDATE auth_attempts SET status='interrupted' WHERE id=? "
                               "AND status IN ('starting','awaiting_user','exchanging')",
                               (attempt_id,))
            self._browser_attempts.clear()
            self._grantbridge_clients.clear()
        if errors:
            raise BridgeError('authentication_interrupted',
                              'The login service closed with an interrupted cleanup.') from None
