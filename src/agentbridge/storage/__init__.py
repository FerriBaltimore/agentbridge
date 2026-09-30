"""Independent optional Store backends and trusted-host migration."""


def migrate_postgres(root, *, operation_id, owner_ref, postgres, verify_quiescence=None):
    from .migration import migrate_postgres as migrate

    return migrate(root, operation_id=operation_id, owner_ref=owner_ref, postgres=postgres,
                   verify_quiescence=verify_quiescence)
