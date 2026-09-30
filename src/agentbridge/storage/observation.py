"""Host-only coherent Store observations, without attesting an external SQL backup."""

from ..checkpoint import native, state
from ..checkpoint.snapshot import coverage


def observe_root(root, *, format_version, params):
    """Inspect existing authority without Store bootstrap, writes or a writer barrier."""
    from .inspection import connect_selected

    with connect_selected(root) as connection:
        if connection is None:
            native.fail('checkpoint_incomplete')
        backend = getattr(connection, 'backend', 'sqlite')
        return observe_connection(connection, backend, format_version=format_version, params=params)


def observe(store, *, format_version, params):
    if store.storage.name == 'postgresql':
        return observe_root(store.root, format_version=format_version, params=params)
    with store.connect() as connection:
        return observe_connection(connection, store.storage.name,
                                  format_version=format_version, params=params)


def observe_connection(connection, backend, *, format_version, params):
    if format_version != '1' or not isinstance(params, dict) or set(params) != set(state.IDENTITY_KEYS):
        native.fail('checkpoint_invalid')
    connection.execute('BEGIN')
    binding = state.identity(connection)
    if not binding['enabled'] or any(params[key] != binding[key] for key in state.IDENTITY_KEYS):
        native.fail('checkpoint_scope_mismatch')
    result = {'format_version': '1', **{key: binding[key] for key in state.IDENTITY_KEYS},
              'backend': backend, 'store_schema': connection.execute(
                  'SELECT version FROM metadata').fetchone()[0],
              'cursor': state.cursor(connection), 'coverage': coverage(connection)}
    if backend == 'postgresql':
        # This is the next WAL insertion position after preceding Store commits.
        # It is observation evidence, not a recovery frontier or protected receipt.
        # The host must bind it to its physical cluster/timeline and verified chain.
        row = connection.execute('SELECT current_database(),pg_current_wal_insert_lsn()::text,'
                                 'pg_is_in_recovery()').fetchone()
        if row[2]:
            native.fail('checkpoint_busy')
        result['sql_position'] = {'database': row[0], 'schema': connection.schema,
                                  'lsn': row[1], 'boundary': 'next_record_exclusive'}
    return result
