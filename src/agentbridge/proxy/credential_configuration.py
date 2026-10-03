"""Trusted SDK inventory of credential components, including conservative legacy refusal."""

import json
import hashlib
from pathlib import Path

from ..bundle import resolve_grantbridge_adapter
from ..bundle.grantbridge_legacy import verified_legacy_adapter
from ..errors import BridgeError
from .managed import ManagedProxyClient


def unsupported():
    raise BridgeError('credential_snapshot_unsupported',
                      'Legacy or unmanaged credentials need an explicit host migration.')


def configuration(store):
    accounts = []
    expected_path = Path(resolve_grantbridge_adapter(store.root))
    expected = expected_path.read_bytes()
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
        # The pinned browser runtime keeps login state only in its private temporary directory.
        # Admit the installed runtime or the exact verified stateless 2.9.1 closure.
        for row in db.execute('SELECT r.connection FROM auth_proxy_routes r WHERE NOT EXISTS '
                '(SELECT 1 FROM auth_attempts a JOIN retired_accounts t ON t.account_id=a.account_id '
                'WHERE a.id=r.attempt_id AND t.proxy_retired_at IS NOT NULL)'):
            connection = json.loads(row[0])
            adapter = Path(connection.get('adapter', ''))
            if connection.get('data_dir') is not None:
                unsupported()
            current = (adapter == expected_path and adapter.is_absolute()
                       and not adapter.is_symlink() and adapter.is_file()
                       and adapter.stat().st_size == len(expected)
                       and hashlib.sha256(adapter.read_bytes()).digest() == expected_digest)
            if not current and not verified_legacy_adapter(adapter, store.root):
                unsupported()
    for root in (store.root / 'grantbridge', Path.home() / '.local/state/grantbridge'):
        if root.exists():
            unsupported()
    return {'format_version': '1', 'cliproxyapi': {'configured': bool(accounts),
                                                 'account_ids': accounts},
            'grantbridge_login': {'configured': False}}


def _alias_digest(alias):
    """Read a host-approved file without following a link or trusting adjacent modules."""
    import os
    import stat

    if (not isinstance(alias, str) or not alias or len(alias) > 4096
            or '\x00' in alias):
        unsupported()
    path = Path(alias)
    try:
        if not path.is_absolute() or str(path) != alias or path.resolve(strict=True) != path:
            unsupported()
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
            details = os.fstat(source.fileno())
            readonly_mount = os.fstatvfs(source.fileno()).f_flag & os.ST_RDONLY
            if (not stat.S_ISREG(details.st_mode) or details.st_size > 65536
                    or (details.st_mode & 0o222 and not readonly_mount)):
                unsupported()
            content = source.read(65537)
            if len(content) > 65536:
                unsupported()
            return hashlib.sha256(content).hexdigest()
    except (OSError, ValueError, RuntimeError):
        unsupported()


def normalize_login_adapters(store, aliases):
    """Explicit host-only upgrade of known aliases; inventory remains read-only."""
    from ..bundle import resolve_binary
    from ..bundle.grantbridge_legacy import FILES
    from ..checkpoint.state import identity
    from .credential_barrier import require_account

    if (not isinstance(aliases, (list, tuple)) or len(aliases) > 8
            or not all(isinstance(alias, str) for alias in aliases)
            or len(set(aliases)) != len(aliases)):
        unsupported()
    if not aliases:
        return {'normalized': 0}
    adapter = str(resolve_grantbridge_adapter(store.root))
    allowed = {hashlib.sha256(Path(adapter).read_bytes()).hexdigest(),
               FILES['scripts/agentbridge-proxy-adapter.mjs']}
    for alias in aliases:
        if _alias_digest(alias) not in allowed:
            unsupported()
    node = str(resolve_binary('node', store.root))
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        updates = []
        rows = db.execute('SELECT r.attempt_id,r.connection,a.account_id '
                          'FROM auth_proxy_routes r LEFT JOIN auth_attempts a '
                          'ON a.id=r.attempt_id').fetchall()
        for row in rows:
            connection = json.loads(row['connection'])
            if connection.get('adapter') not in aliases:
                continue
            if connection.get('data_dir') is not None or row['account_id'] is None:
                unsupported()
            if identity(db)['recovery_held']:
                raise BridgeError('credential_snapshot_pending', 'Store recovery remains held.')
            require_account(db, row['account_id'])
            active = db.execute(
                "SELECT 1 FROM auth_attempts WHERE account_id=? AND status NOT IN "
                "('failed','cancelled','expired','interrupted','abandoned','revoked',"
                "'replaced','bound','usable')", (row['account_id'],)).fetchone()
            if active:
                raise BridgeError('credential_snapshot_pending',
                                  'Finish account authentication before normalizing its adapter.')
            updated = {**connection, 'adapter': adapter, 'node': node}
            if updated != connection:
                updates.append((json.dumps(updated), row['attempt_id']))
        for connection, attempt_id in updates:
            db.execute('UPDATE auth_proxy_routes SET connection=? WHERE attempt_id=?',
                       (connection, attempt_id))
    return {'normalized': len(updates)}
