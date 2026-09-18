"""Public value objects. Credential values are never part of these records."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any

from .errors import BridgeError

ENGINES = ("codex", "claude", "cursor")
TERMINAL = frozenset(("completed", "failed", "cancelled", "interrupted", "incomplete"))


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise BridgeError("invalid_id", "Identifiers must be 1-128 letters, digits, dots, underscores or hyphens.")
    return value


def account_name_key(value: str) -> str:
    """Return the case-insensitive key used to keep account names unique."""
    return value.strip().casefold()


@dataclass(frozen=True)
class Account:
    id: str
    engine: str
    home: str | None = None
    name: str | None = None
    email: str | None = None
    env_names: tuple[str, ...] = ()
    key_env: str | None = None
    command: tuple[str, ...] = ()

    def __post_init__(self):
        identifier(self.id)
        if self.engine not in ENGINES:
            raise BridgeError("invalid_engine", "Choose codex, claude or cursor.")
        if self.name is not None:
            if not isinstance(self.name, str) or not self.name.strip() or len(self.name.strip()) > 128:
                raise BridgeError("invalid_name", "Account names must contain 1-128 non-space characters.")
            object.__setattr__(self, "name", self.name.strip())
        if self.engine != "cursor" and not self.home:
            raise BridgeError("account_home_required", "Codex and Claude accounts require an explicit native home.")
        if self.home:
            object.__setattr__(self, "home", str(Path(self.home).expanduser().resolve()))
        object.__setattr__(self, "env_names", tuple(self.env_names))
        object.__setattr__(self, "command", tuple(self.command))
        for key in (*self.env_names, *((self.key_env,) if self.key_env else ())):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
                raise BridgeError("invalid_environment", "Use environment variable names, not credential values.")
        if any(not isinstance(x, str) or not x or '\0' in x for x in self.command):
            raise BridgeError("invalid_command", "Command must be a list of nonempty arguments, never shell text.")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunOptions:
    timeout: float = 600
    stop_grace: float = 3
    sandbox: str = "read-only"
    permission_mode: str = "dontAsk"
    allowed_tools: tuple[str, ...] = ()
    effort: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    collect_usage: bool = False

    def __post_init__(self):
        if not 0 < self.timeout <= 86400 or not 0 <= self.stop_grace <= 60:
            raise BridgeError("invalid_timeout", "Timeout must be in (0, 86400], stop grace in [0, 60].")
        if self.sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            raise BridgeError("invalid_sandbox", "Unknown sandbox policy.")
        if self.permission_mode not in ("dontAsk", "default", "acceptEdits", "plan", "bypassPermissions"):
            raise BridgeError("invalid_permissions", "Unknown Claude permission mode.")
        if self.max_turns is not None and self.max_turns < 1:
            raise BridgeError("invalid_budget", "max_turns must be positive.")
        if self.max_budget_usd is not None and self.max_budget_usd <= 0:
            raise BridgeError("invalid_budget", "max_budget_usd must be positive.")
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))


@dataclass(frozen=True)
class Event:
    seq: int
    run_id: str
    session_id: str
    kind: str
    at: float
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capabilities:
    engine: str
    execution: bool = True
    resume: bool = True
    cancellation: bool = True
    native_transfer: bool = False
    portable_context: bool = True
    subagents: str = "partial"
    token_usage: str = "partial"
    account_quota: str = "unsupported"
    monetary_cost: str = "unsupported"


CAPABILITIES = {
    "codex": Capabilities("codex", native_transfer=True, account_quota="provider_and_local_observation"),
    "claude": Capabilities("claude", native_transfer=True, account_quota="oauth_reader", monetary_cost="provider_reported"),
    "cursor": Capabilities("cursor", monetary_cost="optional_sdk_query"),
}
