"""Choose the Codex executable for an account without changing custom launchers."""


def codex_argv(account, state_root=None):
    """Return the account launcher or the managed Codex executable.

    Callers in a Bridge operation provide its state root. A missing root keeps
    argument-building fixtures and historical direct adapters independent of
    runtime extraction.
    """
    if account.command:
        return list(account.command)
    if state_root is None:
        return ["codex"]
    from .bundle.runtime import resolve_binary

    return [str(resolve_binary("codex", state_root))]
