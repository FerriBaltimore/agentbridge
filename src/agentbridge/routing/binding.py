"""Keep proxy bindings tied to a completed GrantBridge login."""

import json
import re

from ..errors import BridgeError
from ..models import identifier


_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_ROUTE_FIELDS = ('proxy_base_url', 'key_env', 'management_key_env')


def has_bound_proxy_login(db, account):
    """Check persisted login provenance, including the exact route references."""
    config = account.to_dict() if hasattr(account, 'to_dict') else account
    if (not isinstance(config, dict) or config.get('engine') != 'codex'
            or not config.get('proxy_base_url') or not config.get('provider')
            or not config.get('name')):
        return False
    retired = db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',
                         (config['id'],)).fetchone()
    if retired:
        return False
    rows = db.execute('''SELECT a.engine,a.name,a.grantbridge_id,a.data,r.config
        FROM auth_attempts a JOIN auth_proxy_routes r ON r.attempt_id=a.id
        WHERE a.account_id=? AND a.status='bound' ''', (config['id'],))
    for row in rows:
        if (row['engine'] != config['provider'] or row['name'] != config['name']
                or not row['grantbridge_id']):
            continue
        try:
            attempt = json.loads(row['data'])
            route = json.loads(row['config'])
        except (TypeError, ValueError):
            continue
        if (isinstance(attempt, dict) and attempt.get('state') == 'usable'
                and isinstance(route, dict)
                and all(route.get(key) == config.get(key) for key in _ROUTE_FIELDS)):
            return True
    return False


class ProxyBindingStoreMixin:
    def proxy_login_origin(self, account):
        with self.connect() as db:
            return has_bound_proxy_login(db, account)

    def proxy_binding(self, account_id):
        identifier(account_id)
        with self.connect() as db:
            row = db.execute(
                'SELECT binding_fingerprint,identity_fingerprint FROM proxy_bindings WHERE account_id=?',
                (account_id,)).fetchone()
        return dict(row) if row else None

    def bind_proxy_account(self, account_id, binding_fingerprint, identity_fingerprint):
        """Recheck the login-created binding; observations cannot create one."""
        identifier(account_id)
        for value in (binding_fingerprint, identity_fingerprint):
            if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
                raise BridgeError("proxy_binding_unverified", "The proxy credential identity is unavailable.")
        with self.connect() as db:
            existing = db.execute(
                "SELECT binding_fingerprint,identity_fingerprint FROM proxy_bindings WHERE account_id=?",
                (account_id,)).fetchone()
        if existing is None:
            raise BridgeError("proxy_binding_unverified", "Bind the account through GrantBridge login first.")
        if (existing["binding_fingerprint"] != binding_fingerprint
                or existing["identity_fingerprint"] != identity_fingerprint):
            raise BridgeError("proxy_binding_changed", "The proxy credential binding changed.")
