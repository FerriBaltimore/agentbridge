"""Normalized model catalogs with live and static provider sources."""
from datetime import datetime, timezone
import time

from .account_probe import CodexAppServerProbe
from .errors import BridgeError
from .models import ENGINES


def _static(model_id, display_name):
    return {
        "id": model_id, "display_name": display_name, "description": None,
        "is_default": False, "availability": "unknown", "deprecated": False,
        "retirement": None, "reasoning_efforts": [], "context_windows": [],
        "input_modalities": ["text"], "tool_support": "unknown",
        "subagent_support": "unknown", "service_tiers": [], "provider_extensions": {},
    }


STATIC = {
    "codex": [_static("gpt-5.5", "GPT-5.5")],
    "claude": [_static("claude-sonnet", "Claude Sonnet")],
    "cursor": [_static("auto", "Auto")],
}


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _model(item):
    if not isinstance(item, dict):
        return None
    model_id = item.get("id") or item.get("slug") or item.get("model")
    if not isinstance(model_id, str) or not model_id:
        return None
    effort = item.get("supportedReasoningEfforts") or item.get("supported_reasoning_efforts") or []
    if isinstance(effort, list):
        effort = [value for value in effort if isinstance(value, str)]
    else:
        effort = []
    return {
        "id": model_id,
        "display_name": item.get("displayName") or item.get("display_name") or item.get('label') or model_id,
        "description": item.get("description"),
        "is_default": bool(item.get("isDefault") or item.get("is_default")),
        "availability": item.get("visibility") or item.get("availability") or "unknown",
        "deprecated": bool(item.get("deprecated") or item.get("isDeprecated")),
        "retirement": item.get("retirement") or item.get("upgradeTo"),
        "reasoning_efforts": effort,
        "context_windows": item.get("contextWindow") or item.get("context_windows"),
        "input_modalities": item.get("inputModalities") or item.get("input_modalities") or [],
        "tool_support": item.get("toolSupport"),
        "subagent_support": item.get("subagentSupport"),
        "service_tiers": item.get("serviceTiers") or item.get("service_tiers") or [],
        "provider_extensions": item,
    }


class ModelCatalog:
    """Resolve a safe normalized catalog without spending an inference turn."""

    def __init__(self, accounts):
        self.accounts = accounts

    def list(self, engine, *, account_ref=None, refresh=False, include_hidden=False,
             include_deprecated=False):
        if engine not in ENGINES:
            raise BridgeError("invalid_engine", "Unknown engine.")
        account = self.accounts.resolve(account_ref) if account_ref else None
        if account and account.engine != engine:
            raise BridgeError("invalid_engine", "The account engine does not match the requested engine.")
        source = "static"
        models = STATIC[engine]
        reason = "live_catalog_requires_account"
        observed_at = None
        if account and refresh:
            try:
                from .provider_catalog import models as provider_models
                items = CodexAppServerProbe(account).list_models() if engine == 'codex' else provider_models(account)
                models = [value for item in items if (value := _model(item))]
                source, reason, observed_at = "live", None, _stamp()
            except BridgeError as error:
                reason = error.code
        models = [dict(item, source=source) for item in models]
        if not include_hidden:
            models = [item for item in models if item.get("availability") not in {"hidden", "unlisted"}]
        if not include_deprecated:
            models = [item for item in models if not item.get("deprecated")]
        return {
            "engine": engine,
            "models": models,
            "source": source,
            "stale": source != "live",
            "observed_at": observed_at,
            "supported": source == "live" or bool(models),
            "reason": reason,
            "refresh": bool(refresh),
            "generated_at": _stamp(),
        }
