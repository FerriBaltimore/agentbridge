"""Normalized model catalogs with live and static provider sources."""
from datetime import datetime, timezone

from .account_probe import CodexAppServerProbe
from .errors import BridgeError
from .models import ENGINES


def _static(model_id, display_name):
    value = _model({'id': model_id, 'display_name': display_name})
    value['provider_extensions'] = {}
    return value


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _first(item, *names):
    return next((item[name] for name in names if item.get(name) is not None), None)


def _strings(value):
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item)) if isinstance(value, list) else []


def _positive_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _boolean(item, *names):
    value = _first(item, *names)
    return value if isinstance(value, bool) else None


def _parameters(values):
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            continue
        choices = item.get('values') if isinstance(item.get('values'), list) else []
        result.append({
            'id': item['id'],
            'display_name': _first(item, 'display_name', 'displayName') or item['id'],
            'values': [{'value': choice['value'],
                        'display_name': _first(choice, 'display_name', 'displayName') or choice['value']}
                       for choice in choices if isinstance(choice, dict) and isinstance(choice.get('value'), str)],
        })
    return result


def _variants(values):
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        params = item.get('params') if isinstance(item.get('params'), list) else []
        result.append({
            'display_name': _first(item, 'display_name', 'displayName'),
            'description': item.get('description'),
            'is_default': bool(_boolean(item, 'is_default', 'isDefault')),
            'params': [{'id': param['id'], 'value': param['value']} for param in params
                       if isinstance(param, dict) and isinstance(param.get('id'), str)
                       and isinstance(param.get('value'), str)],
        })
    return result


def _reasoning(item, parameters):
    values = _first(item, 'supportedReasoningEfforts', 'supported_reasoning_efforts',
                    'supportedEffortLevels', 'reasoning_efforts')
    options = []
    for value in values if isinstance(values, list) else []:
        effort = value
        if isinstance(value, dict):
            effort = _first(value, 'reasoningEffort', 'reasoning_effort')
        if isinstance(effort, str) and effort and not any(option['effort'] == effort for option in options):
            options.append({'effort': effort, 'description': value.get('description') if isinstance(value, dict) else None})
    if not options:
        for parameter in parameters:
            if parameter['id'] in {'effort', 'reasoning_effort', 'reasoningEffort'}:
                options.extend({'effort': value['value'], 'description': value['display_name']} for value in parameter['values'])
    return options


def _model(item):
    if not isinstance(item, dict):
        return None
    model_id = item.get("id") or item.get("slug") or item.get("model")
    if not isinstance(model_id, str) or not model_id:
        return None
    parameters = _parameters(item.get('parameters'))
    effort = _reasoning(item, parameters)
    context = _first(item, 'context_windows', 'contextWindow', 'context_window')
    context = context if isinstance(context, list) else [context]
    context = list(dict.fromkeys(value for value in context if _positive_int(value)))
    availability = item.get('visibility') or item.get('availability') or 'unknown'
    if not isinstance(availability, str):
        availability = 'unknown'
    if item.get('hidden') is True:
        availability = 'hidden'
    return {
        "id": model_id,
        "display_name": item.get("displayName") or item.get("display_name") or item.get('label') or model_id,
        "description": item.get("description"),
        "is_default": bool(_boolean(item, "isDefault", "is_default")),
        "availability": availability,
        "deprecated": bool(_boolean(item, "deprecated", "isDeprecated")),
        "retirement": item.get("retirement") or item.get("upgradeTo"),
        "reasoning_efforts": [option['effort'] for option in effort],
        "reasoning_effort_options": effort,
        "default_reasoning_effort": _first(item, 'defaultReasoningEffort', 'default_reasoning_effort'),
        "reasoning_support": _boolean(item, 'supportsEffort', 'reasoning_support'),
        "adaptive_thinking_support": _boolean(item, 'supportsAdaptiveThinking', 'adaptive_thinking_support'),
        "fast_mode_support": _boolean(item, 'supportsFastMode', 'fast_mode_support'),
        "auto_mode_support": _boolean(item, 'supportsAutoMode', 'auto_mode_support'),
        "resolved_model": _first(item, 'resolvedModel', 'resolved_model', 'model'),
        "context_windows": context,
        "max_input_tokens": _positive_int(_first(item, 'maxInputTokens', 'max_input_tokens')),
        "max_output_tokens": _positive_int(_first(item, 'maxOutputTokens', 'max_output_tokens')),
        "input_modalities": _strings(_first(item, 'inputModalities', 'input_modalities')),
        "tool_support": _first(item, 'toolSupport', 'tool_support'),
        "subagent_support": _first(item, 'subagentSupport', 'subagent_support'),
        "service_tiers": item.get("serviceTiers") or item.get("service_tiers") or [],
        "default_service_tier": _first(item, 'defaultServiceTier', 'default_service_tier'),
        "parameters": parameters,
        "variants": _variants(item.get('variants')),
        "provider_extensions": item,
    }


STATIC = {
    "codex": [_static("gpt-5.5", "GPT-5.5")],
    "claude": [_static("claude-sonnet", "Claude Sonnet")],
    "cursor": [_static("auto", "Auto")],
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
        models = [dict(item, source=source, observed_at=observed_at, stale=source != 'live') for item in models]
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
