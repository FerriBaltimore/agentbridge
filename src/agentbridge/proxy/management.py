"""Read bounded, passive observations from one local CLIProxyAPI sidecar.

The Management API returns sensitive credential metadata and raw quota headers.
Only the allowlisted fields below leave this module. No provider request is
made: active quota fetching is deliberately outside this read-only client.
"""

from datetime import datetime, timezone
from hashlib import sha256
import http.client
import json
import math
import os
import re
from urllib.parse import quote, urlsplit

from ..errors import BridgeError
from ..models import model_id
from .route import ProxyRoute


_MAX_BODY = 1024 * 1024
_KEY_ENV = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_USAGE_HEADERS = ("x-codex-primary-used-percent", "x-codex-secondary-used-percent")
_CONFIG_CREDENTIAL_KEYS = (
    "gemini-api-key", "interactions-api-key", "claude-api-key", "codex-api-key",
    "xai-api-key", "meta-api-key", "vertex-api-key", "openai-compatibility",
)


class ManagementClient:
    """Observe the single credential behind a dedicated local proxy route."""

    def __init__(self, route: ProxyRoute, management_key_env: str, *, timeout: float = 3.0):
        if not isinstance(route, ProxyRoute):
            raise BridgeError("invalid_proxy_route", "A proxy route must be a ProxyRoute.")
        if not isinstance(management_key_env, str) or not _KEY_ENV.fullmatch(management_key_env):
            raise BridgeError("invalid_environment", "Use an environment variable name for the management key.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise BridgeError("invalid_timeout", "Management timeout must be in (0, 10].")
        self.route = route
        self.management_key_env = management_key_env
        self.timeout = timeout

    def observe(self) -> dict:
        """Return a sanitized snapshot; missing quota remains unknown (None)."""
        secret = self._secret()
        payload = self._get_json("/v0/management/auth-files", secret)
        files = payload.get("files")
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            raise BridgeError("proxy_binding_unverified", "The local proxy must expose exactly one account.")
        entry = files[0]
        if (entry.get("source") != "file" or entry.get("runtime_only") is not False
                or not isinstance(entry.get("cooldowns"), list)):
            raise BridgeError("proxy_binding_unverified", "The local proxy account source cannot be verified.")
        _verify_config_inventory(self._get_json("/v0/management/config", secret))
        name = entry.get("name")
        if not isinstance(name, str) or not 0 < len(name) <= 200:
            raise BridgeError("proxy_binding_unverified", "The local proxy account has no usable identity reference.")
        model_payload = self._get_json(f"/v0/management/auth-files/models?name={quote(name, safe='')}", secret)
        raw_models = model_payload.get("models")
        if not isinstance(raw_models, list) or len(raw_models) > 500:
            raise BridgeError("proxy_observation_unavailable", "The local proxy model catalog is unavailable.")
        provider = _provider(entry.get("provider"))
        binding_fingerprint, identity_fingerprint = _binding(entry, provider)
        email = entry.get("email")
        if not (isinstance(email, str) and 0 < len(email) <= 320 and email.strip() == email
                and "@" in email and all(32 < ord(char) < 127 for char in email)):
            email = None
        account_percent, account_quota_at = _usage(entry.get("quota")) if provider == "codex" else (None, None)
        model_quotas = entry.get("model_quotas")
        if not isinstance(model_quotas, dict):
            model_quotas = {}
        cooldown_known, cooldowns = _cooldowns(entry.get("cooldowns"))
        models = []
        seen = set()
        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            candidate = raw.get("id")
            try:
                model = model_id(candidate)
            except BridgeError:
                continue
            if model in seen:
                continue
            seen.add(model)
            model_percent, model_quota_at = _usage(model_quotas.get(model)) if provider == "codex" else (None, None)
            if model_percent is None or (account_percent is not None and account_percent > model_percent):
                used_percent, quota_at = account_percent, account_quota_at
                quota_scope = "account" if used_percent is not None else None
            else:
                used_percent, quota_at, quota_scope = model_percent, model_quota_at, "model"
            models.append({"id": model, "used_percent": used_percent,
                           "quota_observed_at": quota_at, "quota_scope": quota_scope,
                           "cooldown_until": cooldowns.get(model) or cooldowns.get("*")})
        return {
            "account_id": self.route.account_id,
            "source": "cliproxy_management",
            "observed_at": _timestamp(payload.get("observed_at")),
            "provider": provider,
            "email": email,
            "binding_fingerprint": binding_fingerprint,
            "identity_fingerprint": identity_fingerprint,
            "status": _status(entry.get("status")),
            "disabled": entry.get("disabled") if isinstance(entry.get("disabled"), bool) else None,
            "unavailable": entry.get("unavailable") if isinstance(entry.get("unavailable"), bool) else None,
            "cooldown_known": cooldown_known,
            "account_used_percent": account_percent,
            "account_quota_observed_at": account_quota_at,
            "models": models,
        }

    def ensure_empty(self) -> None:
        """Require an uncredentialed, isolated sidecar before a new login."""
        if self.credential_count() != 0:
            raise BridgeError("proxy_not_empty", "A new account requires an empty dedicated local proxy.")

    def catalog(self) -> dict:
        """Read optional model controls without changing credential verification."""
        secret = os.environ.get(self.route.key_env)
        if not secret or len(secret) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in secret):
            return {}
        try:
            payload = self._get_json('/v1/models?client_version=pi', secret)
        except BridgeError:
            return {}
        from .model_catalog import catalog_metadata
        return catalog_metadata(payload)

    def credential_count(self) -> int:
        """Read only a bounded count after rejecting hidden credential routes."""
        secret = self._secret()
        payload = self._get_json("/v0/management/auth-files", secret)
        files = payload.get("files")
        if not isinstance(files, list) or len(files) > 1:
            raise BridgeError("proxy_binding_unverified", "The proxy must have at most one account.")
        _verify_config_inventory(self._get_json("/v0/management/config", secret))
        return len(files)

    def _secret(self) -> str:
        secret = os.environ.get(self.management_key_env)
        if not secret or len(secret) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in secret):
            raise BridgeError("credential_unavailable", "The local proxy management key is unavailable.")
        return secret

    def _get_json(self, path: str, secret: str) -> dict:
        endpoint = urlsplit(self.route.base_url)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=self.timeout)
        try:
            connection.request("GET", path, headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise BridgeError("proxy_observation_unavailable", "The local proxy management observation failed.")
            body = response.read(_MAX_BODY + 1)
            if len(body) > _MAX_BODY:
                raise BridgeError("proxy_observation_unavailable", "The local proxy management response is too large.")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise BridgeError("proxy_observation_unavailable", "The local proxy management response is invalid.")
            return payload
        except (OSError, http.client.HTTPException, UnicodeError, ValueError):
            raise BridgeError("proxy_observation_unavailable", "The local proxy management observation failed.") from None
        finally:
            connection.close()


def _timestamp(value) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _verify_config_inventory(config: dict) -> None:
    """Reject hidden config credentials and plugin routes omitted by auth-files."""
    plugins = config.get("plugins")
    if not isinstance(plugins, dict) or plugins.get("enabled") is not False:
        raise BridgeError("proxy_binding_unverified", "The local proxy credential inventory cannot be verified.")
    for key in _CONFIG_CREDENTIAL_KEYS:
        if key not in config or config[key] not in (None, []):
            raise BridgeError("proxy_binding_unverified", "The local proxy contains another credential source.")


def _provider(value) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        return None
    return value


def _binding(entry, provider):
    """Hash stable account evidence without retaining identity or key material."""
    index = entry.get("auth_index")
    if (provider is None or not isinstance(index, str) or not 0 < len(index) <= 256
            or any(ord(char) < 33 or ord(char) > 126 for char in index)):
        raise BridgeError("proxy_binding_unverified", "The proxy credential identity is unavailable.")
    kind = entry.get("account_type")
    if kind == "oauth":
        claims = entry.get("id_token")
        account_id = claims.get("chatgpt_account_id") if provider == "codex" and isinstance(claims, dict) else None
        email = entry.get("email")
        if (provider == "codex" and isinstance(account_id, str) and 0 < len(account_id) <= 256
                and all(32 < ord(char) < 127 for char in account_id)):
            identity = f"chatgpt_account_id:{account_id}"
        elif (provider != "codex" and isinstance(email, str) and 0 < len(email) <= 320
              and email.strip() == email and "@" in email
              and all(ord(char) >= 33 and ord(char) != 127 for char in email)):
            identity = f"email:{email.casefold()}"
        else:
            raise BridgeError("proxy_binding_unverified", "The proxy OAuth identity is unavailable.")
    else:
        raise BridgeError("proxy_binding_unverified", "The proxy credential kind is unavailable.")
    identity_fingerprint = sha256(f"{provider}\0{identity}".encode()).hexdigest()
    binding_fingerprint = sha256(f"{provider}\0{identity}\0{index}".encode()).hexdigest()
    return binding_fingerprint, identity_fingerprint


def _status(value) -> str:
    if isinstance(value, str) and value in {"active", "pending", "refreshing", "error", "disabled"}:
        return value
    return "unknown"


def _usage(raw) -> tuple[float | None, str | None]:
    if not isinstance(raw, dict):
        return None, None
    observed_at = _timestamp(raw.get("observed_at"))
    signals = raw.get("signals")
    if observed_at is None or not isinstance(signals, dict):
        return None, observed_at
    values = []
    for key, value in signals.items():
        if not isinstance(key, str) or key.lower() not in _USAGE_HEADERS or not isinstance(value, str):
            continue
        try:
            percent = float(value)
        except ValueError:
            continue
        if math.isfinite(percent) and 0 <= percent <= 100:
            values.append(percent)
    return (max(values) if values else None), observed_at


def _cooldowns(raw) -> tuple[bool, dict[str, str]]:
    if not isinstance(raw, list):
        return False, {}
    result = {}
    for item in raw:
        if not isinstance(item, dict) or type(item.get("remaining_seconds")) is not int:
            continue
        if item["remaining_seconds"] <= 0:
            continue
        until = _timestamp(item.get("retry_at"))
        if until is None:
            continue
        if item.get("scope") == "credential":
            result["*"] = until
        elif item.get("scope") == "model":
            try:
                model = model_id(item.get("model_key"))
            except BridgeError:
                continue
            result[model] = until
    return True, result
