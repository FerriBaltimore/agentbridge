"""Provider-neutral capability declarations for the public contract."""
from copy import deepcopy

from .models import CAPABILITIES


OPERATIONS = (
    "capabilities.get", "accounts.list", "accounts.status", "accounts.login",
    "accounts.login.start", "accounts.login.status", "accounts.login.check",
    "accounts.login.complete", "accounts.login.cancel",
    "models.list", "usage.get", "accounts.quota.reset", "instances.create", "instances.get",
    "instances.list", "instances.update", "instances.archive", "instances.events", "messages.create",
    "messages.list", "turns.list", "turns.get", "turns.events", "turns.stop",
    "turns.resume", "permissions.respond", "instances.transfer", "instances.export", "recover",
    "error_cases.list", "error_cases.get", "error_cases.diagnose", "error_diagnoses.get",
    "error_proposals.create", "error_proposals.get", "error_proposals.validate",
    "error_rules.get", "error_rules.activate", "error_rules.deactivate",
)


def _support(engine, operation):
    if operation == 'accounts.quota.reset':
        return 'native' if engine == 'codex' else 'unsupported'
    if operation == "permissions.respond":
        return "adapter" if engine in {"codex", "claude"} else "unsupported"
    if operation == "accounts.status":
        return "native" if engine == "codex" else "adapter"
    if operation == "usage.get":
        return "adapter"
    if operation == "models.list":
        return "native" if engine == "codex" else "adapter"
    if operation == "instances.transfer":
        return "native" if engine in {"codex", "claude"} else "portable"
    if operation == "messages.create":
        return "native" if engine == "codex" else "adapter"
    return "adapter"


def _maturity(engine, operation):
    if operation.startswith(('error_cases.', 'error_diagnoses.', 'error_proposals.', 'error_rules.')):
        return 'fixture_tested'
    if operation == 'accounts.quota.reset':
        return 'fixture_tested' if engine == 'codex' else 'unsupported'
    if operation == 'permissions.respond':
        return 'fixture_tested' if engine in {'codex', 'claude'} else 'unsupported'
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
    if operation == 'error_cases.diagnose':
        return ['explicit_cursor_diagnostic_account_required', 'safe_structural_evidence_only',
                'verified_tmpfs_required', 'no_token_or_monetary_budget', 'explicit_rule_review_required']
    if operation == 'error_rules.activate':
        return ['structural_validation_is_not_semantic_verification', 'exact_fingerprint_and_version_only',
                'classification_never_authorizes_retry']
    if operation == 'accounts.quota.reset':
        return ['explicit_consumption_only', 'persistent_idempotency_key_required', 'provider_acceptance_not_run'] if engine == 'codex' else ['provider_reset_api_unavailable']
    if operation == 'models.list' and engine != 'codex':
        return ['catalog_is_not_entitlement_verification', 'static_fallback_on_refresh_failure']
    if operation == 'accounts.status' and engine == 'cursor':
        return ['cached_verification_only', 'no_live_account_reader']
    if operation == 'accounts.status' and engine == 'claude':
        return ['native_status_is_local_only']
    if operation == 'usage.get' and engine == 'cursor':
        return ['sdk_account_quota_unavailable', 'session_usage_is_not_remaining_quota']
    if operation == 'usage.get' and engine == 'claude':
        return ['oauth_usage_is_native_compatibility', 'quota_depends_on_bound_profile']
    if operation == 'accounts.login.check' and engine == 'claude':
        return ['inference_required_for_activation', 'inference_consumes_provider_usage']
    if operation == 'instances.transfer':
        return ['portable_context_is_lossy', 'native_transfer_not_crash_atomic']
    if operation in {'instances.create', 'instances.update'}:
        return ['model_not_verified_at_admission', 'advanced_instance_defaults_unsupported']
    if operation == 'permissions.respond':
        return ['provider_approval_channel_unavailable'] if engine == 'cursor' else [
            'one_request_allow_or_deny_only', 'pending_requests_require_live_worker']
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
                        "maturity": _maturity(name, operation),
                        'limitations': _limitations(name, operation)}
            for operation in OPERATIONS
        }
        data["parameters"] = {
            "model": {"support": "native" if name == "codex" else "adapter",
                      "maturity": "fixture_tested"},
            "effort": {"support": "native" if name == "codex" else "adapter",
                       "maturity": "fixture_tested",
                       "limitations": ['requires_reported_model_parameter'] if name == 'cursor' else []},
            "context_window": {"support": "unsupported", "maturity": "unsupported"},
            "attachments": {"support": "adapter", "maturity": "fixture_tested",
                            "types": ["text", "image"], "max_count": 8, "max_bytes": 5242880,
                            "limitations": ["inline_only", "model_must_support_images", "portable_transfer_omits_content"]},
            "provider_options": {"support": "unsupported", "maturity": "unsupported"},
        }
        data["acceptance"] = {"fixture_tested": True, "provider_tested": False,
                               "provider_versions": []}
        data['requirements'] = ['configured_grantbridge', 'explicit_account'] + (
            ['cursor_sdk_installed', 'explicit_model'] if name == 'cursor' else ['native_cli_installed'])
        result[name] = data
    return result[name] if engine else result
