"""Public value objects. Credential values are never part of these records."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
import math
import re
from typing import Any

from .errors import BridgeError

ENGINES = ("codex", "claude")
TERMINAL = frozenset(("completed", "failed", "cancelled", "interrupted", "incomplete"))


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise BridgeError("invalid_id", "Identifiers must be 1-128 letters, digits, dots, underscores or hyphens.")
    return value


def model_id(value):
    """Provider model IDs are opaque strings, not AgentBridge resource IDs."""
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise BridgeError('invalid_model', 'model must be a nonempty bounded provider identifier.')
    return value


def finite_number(value):
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        return False


def account_name_key(value: str) -> str:
    """Return the case-insensitive key used to keep account names unique."""
    return value.strip().casefold()


def page_values(limit=100, cursor=0, *, allow_none=False):
    if limit is None and allow_none:
        normalized_limit = None
    elif isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1:
        normalized_limit = limit
    else:
        raise BridgeError("invalid_pagination", "limit must be positive.")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        raise BridgeError("invalid_pagination", "cursor must be nonnegative.")
    return normalized_limit, cursor


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
    credential_ref: dict | None = None
    provider: str | None = None
    supported_models: tuple[str, ...] = ()
    proxy_base_url: str | None = None
    management_key_env: str | None = None

    def __post_init__(self):
        identifier(self.id)
        if self.engine not in ENGINES:
            raise BridgeError("invalid_engine", "Choose codex or claude.")
        if self.credential_ref is not None:
            raise BridgeError('invalid_credential_reference', 'Legacy credential references are unsupported.')
        if self.name is not None:
            if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 128:
                raise BridgeError("invalid_name", "Account names must contain 1-128 non-space characters.")
        if not self.home and not self.proxy_base_url:
            raise BridgeError("account_home_required", "An account requires a native home or a proxy route.")
        if self.home:
            object.__setattr__(self, "home", str(Path(self.home).expanduser().resolve()))
        object.__setattr__(self, "env_names", tuple(self.env_names))
        object.__setattr__(self, "command", tuple(self.command))
        if not isinstance(self.supported_models, (list, tuple)):
            raise BridgeError('invalid_models', 'Supported models must be a list of model IDs.')
        object.__setattr__(self, "supported_models", tuple(self.supported_models))
        for key in (*self.env_names, *((self.key_env,) if self.key_env else ()),
                    *((self.management_key_env,) if self.management_key_env else ())):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
                raise BridgeError("invalid_environment", "Use environment variable names, not credential values.")
        if any(not isinstance(x, str) or not x or '\0' in x for x in self.command):
            raise BridgeError("invalid_command", "Command must be a list of nonempty arguments, never shell text.")
        if self.proxy_base_url is not None:
            if self.engine != 'codex' or not self.key_env or not self.provider or not self.supported_models:
                raise BridgeError('invalid_proxy_account', 'A proxy account requires Codex, provider, key environment and models.')
            if self.management_key_env and self.management_key_env in (self.key_env, *self.env_names):
                raise BridgeError('invalid_proxy_account', 'Keep the proxy management key out of the Codex environment.')
            from .proxy import ProxyRoute
            ProxyRoute(self.id, self.proxy_base_url, self.key_env)
        elif self.supported_models or self.provider or self.management_key_env:
            raise BridgeError('invalid_proxy_account', 'Provider and supported models require a proxy route.')
        if self.provider is not None:
            identifier(self.provider)
        if len(self.supported_models) > 500:
            raise BridgeError('invalid_models', 'Configure at most 500 distinct models per account.')
        for model in self.supported_models:
            model_id(model)
        if len(set(self.supported_models)) != len(self.supported_models):
            raise BridgeError('invalid_models', 'Configure distinct models per account.')

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunOptions:
    timeout: float = 600
    stop_grace: float = 3
    sandbox: str = "read-only"
    permission_mode: str = "dontAsk"
    allowed_tools: tuple[str, ...] = ()
    model: str | None = None
    context_window: int | None = None
    effort: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    collect_usage: bool = False
    attachments: tuple[dict, ...] = ()
    context_package_digest: str | None = None
    mcp_binding_digest: str | None = None
    steerable: bool = False

    def __post_init__(self):
        if type(self.steerable) is not bool:
            raise BridgeError('invalid_request', 'steerable must be a boolean.')
        if (not finite_number(self.timeout) or not finite_number(self.stop_grace)
                or not 0 < self.timeout <= 86400 or not 0 <= self.stop_grace <= 60):
            raise BridgeError("invalid_timeout", "Timeout must be in (0, 86400], stop grace in [0, 60].")
        if self.sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            raise BridgeError("invalid_sandbox", "Unknown sandbox policy.")
        if self.permission_mode not in ("dontAsk", "default", "acceptEdits", "plan", "bypassPermissions"):
            raise BridgeError("invalid_permissions", "Unknown permission mode.")
        if self.max_turns is not None and (type(self.max_turns) is not int or self.max_turns < 1):
            raise BridgeError("invalid_budget", "max_turns must be positive.")
        if self.max_budget_usd is not None and (not finite_number(self.max_budget_usd) or self.max_budget_usd <= 0):
            raise BridgeError("invalid_budget", "max_budget_usd must be positive.")
        if self.model is not None:
            model_id(self.model)
        if (self.context_window is not None and
                (type(self.context_window) is not int or not 0 < self.context_window <= 10_000_000)):
            raise BridgeError("invalid_context_window", "context_window must be a positive token count.")
        if self.effort is not None and (not isinstance(self.effort, str) or not self.effort.strip()):
            raise BridgeError('unsupported_parameter', 'effort must be a provider-declared value.')
        for value in (self.context_package_digest, self.mcp_binding_digest):
            if value is not None and (not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value)):
                raise BridgeError('invalid_context', 'Execution context digests must be SHA-256 values.')
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        from .attachments import normalize
        object.__setattr__(self, "attachments", normalize(self.attachments))


@dataclass(frozen=True)
class Event:
    seq: int
    run_id: str
    session_id: str
    kind: str
    at: float
    data: dict[str, Any] = field(default_factory=dict)
