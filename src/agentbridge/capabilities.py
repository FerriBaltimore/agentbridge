"""Provider-neutral capability declarations for the public contract."""
from copy import deepcopy

from .models import CAPABILITIES


OPERATIONS = (
    "capabilities.get", "accounts.list", "accounts.status", "accounts.login",
    "accounts.login.start", "accounts.login.status", "accounts.login.check",
    "accounts.login.complete", "accounts.login.cancel",
    "models.list", "usage.get", "instances.create", "instances.get",
    "instances.list", "instances.update", "instances.archive", "instances.events", "messages.create",
    "messages.list", "turns.list", "turns.get", "turns.events", "turns.stop",
    "turns.resume", "permissions.respond", "instances.transfer", "instances.export", "recover",
)


def _support(engine, operation):
    if operation == "permissions.respond":
        return "unsupported"
    if operation == "accounts.status":
        return "native" if engine == "codex" else "adapter"
    if operation == "usage.get":
        return "adapter"
    if operation == "models.list":
        return "native" if engine == "codex" else "fallback"
    if operation == "instances.transfer":
        return "native" if engine in {"codex", "claude"} else "portable"
    if operation == "messages.create":
        return "native" if engine == "codex" else "adapter"
    return "adapter"


def _maturity(operation):
    if operation == 'permissions.respond':
        return 'unsupported'
    tested = {
        'capabilities.get', 'accounts.list', 'accounts.status', 'accounts.login',
        'accounts.login.start', 'accounts.login.status', 'accounts.login.check',
        'accounts.login.complete', 'accounts.login.cancel', 'models.list', 'usage.get',
        'instances.create', 'instances.get', 'instances.list', 'instances.update',
        'instances.archive', 'instances.events', 'messages.create', 'messages.list',
        'turns.list', 'turns.get', 'turns.events', 'turns.stop', 'instances.transfer',
        'instances.export', 'recover',
    }
    return 'fixture_tested' if operation in tested else 'implemented'


def _limitations(engine, operation):
    if operation == 'models.list' and engine != 'codex':
        return ['static_fallback_only', 'availability_unknown']
    if operation == 'accounts.status' and engine != 'codex':
        return ['cached_verification_only', 'no_live_account_reader']
    if operation == 'usage.get' and engine != 'codex':
        return ['account_usage_unsupported', 'turn_usage_provider_dependent']
    if operation == 'accounts.login.check' and engine == 'claude':
        return ['inference_required_for_activation', 'inference_consumes_provider_usage']
    if operation == 'instances.transfer':
        return ['portable_context_is_lossy', 'native_transfer_not_crash_atomic']
    if operation in {'instances.create', 'instances.update'}:
        return ['model_not_verified_at_admission', 'advanced_instance_defaults_unsupported']
    if operation == 'permissions.respond':
        return ['provider_response_transport_missing']
    return []


def payload(engine=None):
    selected = [engine] if engine else list(CAPABILITIES)
    result = {}
    for name in selected:
        base = CAPABILITIES.get(name)
        if base is None:
            raise KeyError(name)
        data = deepcopy(base.__dict__)
        data["contract_version"] = "v1"
        data["operations"] = {
            operation: {"support": _support(name, operation), "engine": name,
                        "maturity": _maturity(operation),
                        'limitations': _limitations(name, operation)}
            for operation in OPERATIONS
        }
        data["parameters"] = {
            "model": {"support": "native" if name == "codex" else "adapter",
                      "maturity": "fixture_tested"},
            "effort": {"support": "native" if name == "codex" else
                       "adapter" if name == "claude" else "unsupported",
                       "maturity": "fixture_tested" if name != "cursor" else "unsupported"},
            "context_window": {"support": "unsupported", "maturity": "unsupported"},
            "attachments": {"support": "unsupported", "maturity": "unsupported"},
            "provider_options": {"support": "unsupported", "maturity": "unsupported"},
        }
        data["acceptance"] = {"fixture_tested": True, "provider_tested": False,
                               "provider_versions": []}
        data['requirements'] = ['configured_grantbridge', 'explicit_account'] + (
            ['cursor_sdk_installed', 'explicit_model'] if name == 'cursor' else ['native_cli_installed'])
        result[name] = data
    return result[name] if engine else result
