"""Host-only PostgreSQL selection. The connection secret is outside the Store tree."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

from ..errors import BridgeError


def read_private(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise BridgeError('unsafe_store_configuration', 'Store configuration must be private.')
    for parent in path.parents:
        info = parent.stat()
        if (info.st_uid not in {0, os.getuid()}
                or info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise BridgeError('unsafe_store_configuration', 'Store configuration parents must be trusted.')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise BridgeError('unsafe_store_configuration', 'Store configuration must be private.')
        with os.fdopen(descriptor, 'r', closefd=False) as source:
            return source.read(16385)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class PostgresConfiguration:
    schema: str
    conninfo_file: Path
    physical_guard: int | None = None

    def __post_init__(self):
        if not isinstance(self.schema, str) or not re.fullmatch(r'ab_[a-z0-9_]{1,59}', self.schema):
            raise BridgeError('invalid_store_configuration', 'Use a dedicated ab_ Store schema.')
        if (self.physical_guard is not None and
                (type(self.physical_guard) is not int or not -(2**63) <= self.physical_guard < 2**63)):
            raise BridgeError('invalid_store_configuration', 'Physical guard must be a signed bigint.')
        object.__setattr__(self, 'conninfo_file', Path(self.conninfo_file))
        if not self.conninfo_file.is_absolute():
            raise BridgeError('invalid_store_configuration', 'Connection file must be absolute.')

    def document(self):
        value = {'schema': self.schema, 'conninfo_file': str(self.conninfo_file)}
        if self.physical_guard is not None:
            value['physical_guard'] = self.physical_guard
        return value

    def connection_parameters(self):
        try:
            from psycopg import pq
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            raise BridgeError('postgres_dependency_required',
                              'Install AgentBridge with its postgres extra.') from None
        try:
            values = conninfo_to_dict(read_private(self.conninfo_file))
            if not all(values.get(key) for key in ('host', 'dbname', 'user')):
                raise ValueError()
            if any(values.get(key) for key in ('service', 'options', 'passfile')):
                raise ValueError()
            if any(item.envvar and os.environ.get(item.envvar.decode())
                   for item in pq.Conninfo.get_defaults()):
                raise BridgeError('unsafe_store_configuration',
                                  'Remove ambient libpq settings; use the private connection file.')
            values.update(connect_timeout='10', application_name='agentbridge-store',
                          options='-c statement_timeout=15000 -c lock_timeout=10000 '
                                  '-c idle_in_transaction_session_timeout=30000 -c synchronous_commit=on')
            values.setdefault('password', '')
            values['passfile'] = str(self.conninfo_file / 'no-ambient-password')
            values['sslpassword'] = values.get('sslpassword', '')
            return values
        except BridgeError:
            raise
        except Exception:
            raise BridgeError('invalid_store_configuration',
                              'The private PostgreSQL connection file is invalid.') from None
