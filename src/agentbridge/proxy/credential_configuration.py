"""Trusted SDK inventory of credential components, including conservative legacy refusal."""

import json
import hashlib
from pathlib import Path

from ..bundle import resolve_grantbridge_adapter
from ..errors import BridgeError
from .managed import ManagedProxyClient


def unsupported():
    raise BridgeError('credential_snapshot_unsupported',
                      'Legacy or unmanaged credentials need an explicit host migration.')


def configuration(store):
    accounts = []
    expected = Path(resolve_grantbridge_adapter(store.root)).read_bytes()
    expected_digest = hashlib.sha256(expected).digest()
    with store.connect() as db:
        for row in db.execute('SELECT id,config FROM accounts ORDER BY id'):
            retired = db.execute('SELECT proxy_retired_at FROM retired_accounts WHERE account_id=?',
                                 (row['id'],)).fetchone()
            if retired is not None:
                # This is a semantic tombstone, not a claim that a process namespace is closed.
                # Retired secrets must never be part of a recoverable active credential set.
                if retired['proxy_retired_at'] is None:
                    raise BridgeError('credential_snapshot_pending',
                                      'Account retirement has not completed.')
                continue
            if not ManagedProxyClient.is_managed(json.loads(row['config']), row['id']):
                unsupported()
            accounts.append(row['id'])
        # The bundled dependency-free GrantBridge transport opens no vault or browser profile.
        # Historical/custom data directories cannot be asserted absent from filename alone.
        for row in db.execute('SELECT r.connection FROM auth_proxy_routes r WHERE NOT EXISTS '
                '(SELECT 1 FROM auth_attempts a JOIN retired_accounts t ON t.account_id=a.account_id '
                'WHERE a.id=r.attempt_id AND t.proxy_retired_at IS NOT NULL)'):
            connection = json.loads(row[0])
            adapter = Path(connection.get('adapter', ''))
            if (connection.get('data_dir') is not None
                    or not adapter.is_absolute() or adapter.is_symlink() or not adapter.is_file()
                    or adapter.stat().st_size != len(expected)
                    or hashlib.sha256(adapter.read_bytes()).digest() != expected_digest):
                unsupported()
    for root in (store.root / 'grantbridge', Path.home() / '.local/state/grantbridge'):
        if root.exists():
            unsupported()
    return {'format_version': '1', 'cliproxyapi': {'configured': bool(accounts),
                                                 'account_ids': accounts},
            'grantbridge_login': {'configured': False}}
