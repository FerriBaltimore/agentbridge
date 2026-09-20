"""Normalized provider catalogs; unavailable discovery never invents models."""
from datetime import datetime, timezone

from .account_probe import CodexAppServerProbe
from .errors import BridgeError
from .models import ENGINES


def _text(value):
    return value if isinstance(value, str) and 0 < len(value) <= 4096 and all(ord(c) >= 32 and ord(c) != 127 for c in value) else None


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _first(item, *names):
    return next((item[name] for name in names if item.get(name) is not None), None)


def _strings(value):
    return list(dict.fromkeys(item for item in value if _text(item))) if isinstance(value, list) else []


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
        if not isinstance(item, dict) or not _text(item.get('id')):
            continue
        choices = item.get('values') if isinstance(item.get('values'), list) else []
        result.append({
            'id': item['id'],
            'display_name': _text(_first(item, 'display_name', 'displayName')) or item['id'],
            'values': [{'value': choice['value'],
                        'display_name': _text(_first(choice, 'display_name', 'displayName')) or choice['value']}
                       for choice in choices if isinstance(choice, dict) and _text(choice.get('value'))],
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
            'display_name': _text(_first(item, 'display_name', 'displayName')),
            'description': _text(item.get('description')),
            'is_default': _boolean(item, 'is_default', 'isDefault'),
            'params': [{'id': param['id'], 'value': param['value']} for param in params
                       if isinstance(param, dict) and _text(param.get('id'))
                       and _text(param.get('value'))],
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
        if _text(effort) and not any(option['effort'] == effort for option in options):
            options.append({'effort': effort, 'description': _text(value.get('description')) if isinstance(value, dict) else None})
    if not options:
        for parameter in parameters:
            if parameter['id'] in {'effort', 'reasoning_effort', 'reasoningEffort'}:
                options.extend({'effort': value['value'], 'description': value['display_name']} for value in parameter['values'])
    return options


def _model(item):
    if not isinstance(item, dict):
        return None
    model_id = _text(_first(item, 'id', 'slug', 'model'))
    if model_id is None:
        return None
    parameters = _parameters(item.get('parameters'))
    effort = _reasoning(item, parameters)
    context = _first(item, 'context_windows', 'contextWindow', 'context_window')
    context = context if isinstance(context, list) else [context]
    context = list(dict.fromkeys(value for value in context if _positive_int(value)))
    availability = _text(_first(item, 'visibility', 'availability')) or 'unknown'
    if item.get('hidden') is True:
        availability = 'hidden'
    return {
        "id": model_id,
        "display_name": _text(_first(item, 'displayName', 'display_name', 'label')) or model_id,
        "description": _text(item.get("description")),
        "is_default": _boolean(item, "isDefault", "is_default"),
        "availability": availability,
        "deprecated": _boolean(item, "deprecated", "isDeprecated"),
        "retirement": _text(item.get('retirement')),
        "upgrade": _text(_first(item, 'upgrade', 'upgradeTo')),
        "upgrade_info": item['upgradeInfo'] if isinstance(item.get('upgradeInfo'), dict) else None,
        "reasoning_efforts": [option['effort'] for option in effort],
        "reasoning_effort_options": effort,
        "default_reasoning_effort": _text(_first(item, 'defaultReasoningEffort', 'default_reasoning_effort')),
        "reasoning_support": _boolean(item, 'supportsEffort', 'reasoning_support'),
        "adaptive_thinking_support": _boolean(item, 'supportsAdaptiveThinking', 'adaptive_thinking_support'),
        "fast_mode_support": _boolean(item, 'supportsFastMode', 'fast_mode_support'),
        "auto_mode_support": _boolean(item, 'supportsAutoMode', 'auto_mode_support'),
        "resolved_model": _text(_first(item, 'resolvedModel', 'resolved_model', 'model')),
        "context_windows": context,
        "max_input_tokens": _positive_int(_first(item, 'maxInputTokens', 'max_input_tokens')),
        "max_output_tokens": _positive_int(_first(item, 'maxOutputTokens', 'max_output_tokens')),
        "input_modalities": _strings(_first(item, 'inputModalities', 'input_modalities')),
        "tool_support": _boolean(item, 'toolSupport', 'tool_support'),
        "subagent_support": _boolean(item, 'subagentSupport', 'subagent_support'),
        "multi_agent_version": _text(item.get('multiAgentVersion')),
        "personality_support": _boolean(item, 'supportsPersonality'),
        "model_specialty": _text(item.get('modelSpecialty')),
        "service_tiers": _first(item, 'serviceTiers', 'service_tiers') if isinstance(_first(item, 'serviceTiers', 'service_tiers'), list) else [],
        "default_service_tier": _text(_first(item, 'defaultServiceTier', 'default_service_tier')),
        "parameters": parameters,
        "variants": _variants(item.get('variants')),
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
        if any(type(flag) is not bool for flag in (refresh, include_hidden, include_deprecated)):
            raise BridgeError('invalid_input', 'Catalog query flags must be booleans.')
        account = self.accounts.resolve(account_ref) if account_ref else None
        if account and account.engine != engine:
            raise BridgeError("invalid_engine", "The account engine does not match the requested engine.")
        source = "static"
        models = []
        reason = 'live_catalog_refresh_required' if account else 'live_catalog_requires_account'
        observed_at = None
        compatibility = None
        if account and refresh:
            try:
                from .provider_contracts import ContractRegistry
                compatibility = ContractRegistry(self.accounts.store).check(account)
                if not compatibility['native_operations_allowed']:
                    raise BridgeError('provider_contract_unverified', 'The installed release needs contract review.')
                from .provider_catalog import models as provider_models
                items = CodexAppServerProbe(account).list_models() if engine == 'codex' else provider_models(account)
                if not isinstance(items, (list, tuple)):
                    raise BridgeError('provider_protocol_error', 'The provider returned an invalid model catalog.')
                models = [value for item in items if (value := _model(item))]
                if len(models) != len(items) or len({item['id'] for item in models}) != len(models):
                    models = []
                    raise BridgeError('provider_protocol_error', 'The provider returned malformed or duplicate model entries.')
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
            "supported": source == "live",
            "reason": reason,
            "refresh": bool(refresh),
            "generated_at": _stamp(),
            "provider_compatibility": compatibility,
        }
