"""Keep one Codex account bound to one explicitly configured local proxy."""

from dataclasses import dataclass
import ipaddress
import json
import re
from urllib.parse import urlsplit

from ..errors import BridgeError
from ..models import identifier


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_PROVIDER_NAME = "agentbridge_local_proxy"


@dataclass(frozen=True)
class ProxyRoute:
    """Public references for a dedicated sidecar; never contains a key value.

    The caller must ensure that the proxy instance itself has exactly one
    upstream account. A URL cannot prove the sidecar's internal configuration.
    """

    account_id: str
    base_url: str
    key_env: str

    def __post_init__(self):
        identifier(self.account_id)
        if not isinstance(self.key_env, str) or not _ENV_NAME.fullmatch(self.key_env):
            raise BridgeError("invalid_environment", "Use an environment variable name for the proxy key.")
        if not isinstance(self.base_url, str) or any(c.isspace() for c in self.base_url):
            raise BridgeError("invalid_proxy_endpoint", "The proxy endpoint must be a local HTTP /v1 URL.")
        try:
            parsed = urlsplit(self.base_url)
            host = parsed.hostname
            port = parsed.port
            address = ipaddress.ip_address(host or "")
        except ValueError:
            raise BridgeError("invalid_proxy_endpoint", "The proxy endpoint must be a local HTTP /v1 URL.") from None
        if (parsed.scheme != "http" or not address.is_loopback or port is None or port == 0
                or parsed.username is not None or parsed.password is not None
                or parsed.path != "/v1" or parsed.query or parsed.fragment):
            raise BridgeError("invalid_proxy_endpoint", "The proxy endpoint must be a local HTTP /v1 URL.")
        host_text = f"[{address}]" if address.version == 6 else str(address)
        if self.base_url != f"http://{host_text}:{port}/v1":
            raise BridgeError("invalid_proxy_endpoint", "Use the canonical local HTTP /v1 URL.")


def validate_unique_endpoints(routes):
    """Reject two account references to the same supposedly dedicated proxy."""
    owners = {}
    for route in routes:
        if not isinstance(route, ProxyRoute):
            raise BridgeError("invalid_proxy_route", "A proxy route must be a ProxyRoute.")
        previous = owners.get(route.base_url)
        if previous is not None and previous != route.account_id:
            raise BridgeError("proxy_endpoint_shared", "A dedicated proxy endpoint is already bound to another account.")
        owners[route.base_url] = route.account_id


def codex_overrides(route: ProxyRoute) -> tuple[str, ...]:
    """Return Codex CLI config arguments for the Responses wire protocol."""
    if not isinstance(route, ProxyRoute):
        raise BridgeError("invalid_proxy_route", "A proxy route must be a ProxyRoute.")
    values = (
        ("model_provider", _PROVIDER_NAME),
        (f"model_providers.{_PROVIDER_NAME}.name", "AgentBridge local proxy"),
        (f"model_providers.{_PROVIDER_NAME}.base_url", route.base_url),
        (f"model_providers.{_PROVIDER_NAME}.env_key", route.key_env),
        (f"model_providers.{_PROVIDER_NAME}.wire_api", "responses"),
    )
    return tuple(part for key, value in values for part in ("-c", f"{key}={json.dumps(value)}"))
