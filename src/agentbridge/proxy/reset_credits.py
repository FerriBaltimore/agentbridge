"""Codex earned reset transport through one verified CLIProxyAPI credential.

Codex's app-server is the public provider contract. The pinned Codex backend
client uses these WHAM requests; CLIProxyAPI's management api-call substitutes
the selected OAuth access token without exposing it to AgentBridge. Callers
must durably save a redemption attempt and its idempotency key before consume.
"""

from datetime import datetime, timezone
import json
import re

from ..errors import BridgeError
from .management import ManagementClient, _binding, _verify_config_inventory


_BASE = "https://chatgpt.com/backend-api/wham"
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_OUTCOMES = {
    "reset": "reset",
    "already_redeemed": "already_redeemed",
    "nothing_to_reset": "nothing_to_reset",
    "no_credit": "no_credit",
}


def _opaque(value, maximum):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and all(32 < ord(char) < 127 and not char.isspace() for char in value))


def _stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bound_account(client, expected_binding_fingerprint):
    """Resolve the exact OAuth credential, without returning provider secrets."""
    if not isinstance(client, ManagementClient):
        raise BridgeError("invalid_proxy_route", "A local proxy management client is required.")
    if (not isinstance(expected_binding_fingerprint, str)
            or not _FINGERPRINT.fullmatch(expected_binding_fingerprint)):
        raise BridgeError("proxy_binding_unverified", "The account binding is unavailable.")
    secret = client._secret()
    payload = client._get_json("/v0/management/auth-files", secret)
    files = payload.get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise BridgeError("proxy_binding_unverified", "The local proxy must expose one account.")
    entry = files[0]
    if (entry.get("provider") != "codex" or entry.get("source") != "file"
            or entry.get("runtime_only") is not False
            or entry.get("status") != "active" or entry.get("disabled") is not False
            or type(entry.get("unavailable")) is not bool
            or not isinstance(entry.get("cooldowns"), list)):
        raise BridgeError("proxy_binding_unverified", "The Codex proxy account is not verified.")
    _verify_config_inventory(client._get_json("/v0/management/config", secret))
    binding, _ = _binding(entry, "codex")
    if binding != expected_binding_fingerprint:
        raise BridgeError("proxy_binding_changed", "The Codex proxy account binding changed.")
    claims = entry.get("id_token")
    account_id = claims.get("chatgpt_account_id") if isinstance(claims, dict) else None
    if not _opaque(account_id, 256):
        raise BridgeError("proxy_binding_unverified", "The Codex account identity is unavailable.")
    return secret, entry["auth_index"], account_id


def _call(client, secret, index, account_id, method, path, data=None):
    headers = {"Authorization": "Bearer $TOKEN$", "Accept": "application/json",
               "ChatGPT-Account-Id": account_id, "User-Agent": "codex-cli"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = {"auth_index": index, "method": method, "url": f"{_BASE}{path}",
               "header": headers}
    if data is not None:
        request["data"] = json.dumps(data, separators=(",", ":"))
    response = client._post_json("/v0/management/api-call", secret, request)
    if response.get("status_code") != 200:
        raise BridgeError("upstream_reset_unavailable", "Codex did not provide reset information.")
    body = response.get("body")
    if not isinstance(body, str) or len(body) > 262144:
        raise BridgeError("upstream_reset_unavailable", "The Codex reset response is unavailable.")
    try:
        decoded = json.loads(body)
    except ValueError:
        decoded = None
    if not isinstance(decoded, dict):
        raise BridgeError("upstream_reset_unavailable", "The Codex reset response is invalid.")
    return decoded


def _count(raw):
    count = raw.get("available_count") if isinstance(raw, dict) else None
    return count if type(count) is int and 0 <= count <= 1000000 else None


def _credit(raw):
    if not isinstance(raw, dict) or not _opaque(raw.get("id"), 512):
        return None
    granted_at = _stamp(raw.get("granted_at"))
    expires_at = _stamp(raw.get("expires_at")) if raw.get("expires_at") is not None else None
    if granted_at is None or (raw.get("expires_at") is not None and expires_at is None):
        return None
    title = raw.get("title")
    description = raw.get("description")
    title = title if isinstance(title, str) and len(title) <= 160 and title.isprintable() else None
    description = (description if isinstance(description, str) and len(description) <= 512
                   and description.isprintable() else None)
    return {"id": raw["id"],
            "reset_type": ("codex_rate_limits" if raw.get("reset_type") == "codex_rate_limits"
                           else "unknown"),
            "status": (raw["status"] if raw.get("status") in {"available", "redeeming", "redeemed"}
                       else "unknown"),
            "granted_at": granted_at, "expires_at": expires_at,
            "title": title, "description": description}


def _details(raw):
    count = _count(raw)
    credits = raw.get("credits") if isinstance(raw, dict) else None
    if count is None or not isinstance(credits, list) or len(credits) > 100:
        return None
    projected = [_credit(item) for item in credits]
    if any(item is None for item in projected):
        return None
    return count, projected


class CodexResetProxy:
    """Read or redeem resets for a previously bound, dedicated Codex sidecar."""

    def __init__(self, client: ManagementClient, expected_binding_fingerprint: str):
        self.client = client
        self.expected_binding_fingerprint = expected_binding_fingerprint

    def read(self):
        secret, index, account_id = _bound_account(
            self.client, self.expected_binding_fingerprint)
        count, credits = None, None
        try:
            usage = _call(self.client, secret, index, account_id, "GET", "/usage")
            observed_account = usage.get("account_id")
            if observed_account is not None and observed_account != account_id:
                raise BridgeError("proxy_binding_changed", "The Codex backend account changed.")
            count = _count(usage.get("rate_limit_reset_credits"))
        except BridgeError as error:
            if error.code == "proxy_binding_changed":
                raise
        try:
            details = _details(_call(self.client, secret, index, account_id, "GET",
                                     "/rate-limit-reset-credits"))
            if details is not None:
                count, credits = details
        except BridgeError:
            pass
        if count is None:
            raise BridgeError("upstream_reset_unavailable", "Codex reset credits are unavailable.")
        _bound_account(self.client, self.expected_binding_fingerprint)
        return {"available_count": count, "status": "available" if count else "none",
                "credits": credits, "observed_at": _now()}

    def consume(self, idempotency_key, credit_id=None):
        if not _opaque(idempotency_key, 200):
            raise BridgeError("invalid_idempotency_key", "Use a nonempty opaque idempotency key.")
        if credit_id is not None and not _opaque(credit_id, 512):
            raise BridgeError("invalid_credit_id", "Use an opaque credit ID from Codex.")
        secret, index, account_id = _bound_account(
            self.client, self.expected_binding_fingerprint)
        data = {"redeem_request_id": idempotency_key}
        if credit_id is not None:
            data["credit_id"] = credit_id
        try:
            response = _call(self.client, secret, index, account_id, "POST",
                             "/rate-limit-reset-credits/consume", data)
            outcome = _OUTCOMES.get(response.get("code"))
            if outcome is None:
                raise BridgeError("upstream_reset_unavailable", "Codex returned no reset outcome.")
            windows = response.get("windows_reset")
            if windows is not None and (type(windows) is not int or not 0 <= windows <= 1000000):
                raise BridgeError("upstream_reset_unavailable", "Codex returned an invalid reset result.")
            _bound_account(self.client, self.expected_binding_fingerprint)
        except BridgeError:
            raise BridgeError("reset_outcome_unknown", "The Codex reset outcome is unknown.",
                              phase="redemption", outcome="unknown") from None
        result = {"outcome": outcome}
        if windows is not None:
            result["windows_reset"] = windows
        return result


def read(client: ManagementClient, expected_binding_fingerprint: str):
    return CodexResetProxy(client, expected_binding_fingerprint).read()


def consume(client: ManagementClient, expected_binding_fingerprint: str,
            idempotency_key: str, credit_id: str | None = None):
    return CodexResetProxy(client, expected_binding_fingerprint).consume(idempotency_key, credit_id)
