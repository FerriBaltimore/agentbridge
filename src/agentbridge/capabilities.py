"""Provider-neutral capability declarations for the public contract."""

OPERATIONS = (
    "contracts.list", "contracts.get", "contracts.check", "contracts.inspect",
    "capabilities.get", "accounts.list", "accounts.status", "accounts.delete",
    "accounts.pause", "accounts.resume",
    "accounts.usage", "accounts.usage_history",
    "accounts.login",
    "accounts.login.list", "accounts.login.start", "accounts.login.status", "accounts.login.check",
    "accounts.login.complete", "accounts.login.cancel", "accounts.login.callback",
    "models.list", "usage.get", "usage.history", "accounts.reset_credits",
    "accounts.quota.reset",
    "instances.create", "instances.get",
    "instances.list", "instances.update", "instances.archive", "instances.delete",
    "instances.discard_evaluation",
    "instances.events", "messages.create",
    "messages.list", "turns.list", "turns.get", "turns.events", "turns.stop",
    "turns.resume", "permissions.respond", "instances.transfer", "instances.export", "recover",
    "error_cases.list", "error_cases.get", "error_cases.diagnose", "error_diagnoses.get",
    "error_proposals.create", "error_proposals.get", "error_proposals.validate",
    "error_rules.get", "error_rules.activate", "error_rules.deactivate",
)


def proxy_payload(*, include_parameters=True):
    """Declare only the supported Codex-to-local-proxy execution surface."""
    unsupported = {'error_cases.diagnose'}
    operations = {}
    for operation in OPERATIONS:
        enabled = operation not in unsupported
        item = {'support': 'adapter' if enabled else 'unsupported',
                'maturity': 'fixture_tested' if enabled else 'unsupported',
                'limitations': []}
        if operation == 'accounts.login.list':
            item['limitations'] = ['local_private_store_only',
                                   'interrupted_attempts_only', 'bounded_to_100']
        elif operation == 'accounts.login.callback':
            item['limitations'] = ['one_use_remote_browser_redirect',
                                   'codex_and_claude_only', 'live_oauth_acceptance_pending']
        elif operation.startswith('accounts.login'):
            item['limitations'] = ['local_same_host_browser', 'live_oauth_acceptance_pending']
        elif operation == 'accounts.delete':
            item['limitations'] = ['local_retirement_only', 'upstream_credential_remains']
        elif operation in {'accounts.pause', 'accounts.resume'}:
            item['limitations'] = ['new_work_only', 'active_turns_continue',
                                   'upstream_credential_remains']
        elif operation == 'accounts.reset_credits':
            item['limitations'] = ['codex_proxy_accounts_only', 'explicit_refresh_for_provider_read',
                                   'private_upstream_endpoint', 'live_provider_acceptance_pending']
        elif operation == 'accounts.quota.reset':
            item['limitations'] = ['codex_proxy_accounts_only', 'explicit_redemption_only',
                                   'fresh_observation_ref_required', 'durable_idempotency_key_required',
                                   'unknown_outcome_requires_same_key',
                                   'private_upstream_endpoint', 'live_provider_acceptance_pending']
        elif operation.startswith('contracts.'):
            item['limitations'] = ['schema_audit_only', 'execution_engine_is_codex']
        elif operation == 'models.list':
            item['limitations'] = ['local_catalog_is_not_provider_entitlement']
        elif operation == 'instances.transfer':
            item['support'] = 'portable'
            item['limitations'] = ['bounded_context', 'explicit_omissions']
        elif operation == 'instances.discard_evaluation':
            item['limitations'] = ['explicit_evaluation_instances_only',
                                   'terminal_processes_required', 'live_provider_acceptance_pending']
        elif operation == 'instances.delete':
            item['limitations'] = ['local_evidence_only', 'terminal_processes_required',
                                   'evaluation_instances_use_discard_evaluation']
        operations[operation] = item
    value = {'contract_version': 'v2',
             'declaration_scope': 'adapter_implementation',
             'execution_engine': 'codex',
             'route': 'local_cliproxyapi',
             'providers': ['codex', 'claude', 'grok'],
             'runtime_provider_support_verified': False,
             'operations': operations}
    if include_parameters:
        value['parameters'] = {
            'model': {'support': 'adapter', 'maturity': 'fixture_tested'},
            'effort': {'support': 'adapter', 'maturity': 'fixture_tested',
                       'limitations': ['provider_model_may_ignore_effort']},
            'attachments': {'support': 'adapter', 'maturity': 'fixture_tested',
                            'limitations': ['model_support_varies']},
            'context_window': {'support': 'adapter', 'maturity': 'fixture_tested',
                               'limitations': ['model_maximum_must_be_observed',
                                               'provider_may_reject_override']},
            'context_package': {'support': 'adapter', 'maturity': 'fixture_tested',
                                'limitations': ['bounded_immutable_selection', 'live_provider_acceptance_pending']},
            'mcp': {'support': 'adapter', 'maturity': 'fixture_tested',
                    'limitations': ['private_unix_socket', 'live_provider_acceptance_pending']},
            'permission_mode': {'support': 'adapter', 'maturity': 'fixture_tested',
                                'values': [
                                    {'value': 'dontAsk', 'display_name': 'No prompts'},
                                    {'value': 'default', 'display_name': 'Ask before actions'},
                                ],
                                'limitations': ['interactive_codex_transport']},
            'allowed_tools': {'support': 'unsupported', 'maturity': 'unsupported'},
            'max_budget': {'support': 'unsupported', 'maturity': 'unsupported'},
            'provider_options': {'support': 'unsupported', 'maturity': 'unsupported'},
        }
    return value
