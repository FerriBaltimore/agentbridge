"""Administrative native durability; independent of any hosting or object-storage service."""


def observe_store(root, *, format_version, params):
    """Trusted-host read-only observation; never bootstrap or migrate this Store."""
    from ..storage.observation import observe_root

    return observe_root(root, format_version=format_version, params=params)


def restore_postgres(destination, snapshot, *, postgres, owner_ref, operation_id,
                     workspace_paths=None):
    """Bind already recovered SQL to new trusted host authority, preserving a recovery hold."""
    from .postgres_restore import restore_postgres as restore

    return restore(destination, snapshot, postgres=postgres, owner_ref=owner_ref,
                   operation_id=operation_id, workspace_paths=workspace_paths)
