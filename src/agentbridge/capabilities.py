"""Provider-neutral capability declarations for the public contract."""

OPERATIONS = (
    "contracts.list", "contracts.get", "contracts.check", "contracts.inspect",
    "capabilities.get", "accounts.list", "accounts.status", "accounts.delete",
    "accounts.pause", "accounts.resume",
    "accounts.usage", "accounts.usage_history",
    "accounts.login",
    "accounts.login.list", "accounts.login.start", "accounts.login.status", "accounts.login.check",
    "accounts.login.complete", "accounts.login.cancel", "accounts.login.callback",
    "models.list", "usage.get", "usage.history", "accounts.quota.reset",
    "instances.create", "instances.get",
    "instances.list", "instances.update", "instances.archive", "instances.delete",
    "instances.discard_evaluation",
    "instances.events", "messages.create",
    "messages.get", "queues.list", "queues.add", "queues.move", "queues.delete",
    "queues.dispatch", "queues.pause", "queues.resume",
    "messages.list", "turns.list", "turns.get", "turns.events", "turns.stop",
    "turns.resume", "permissions.respond", "instances.transfer", "instances.export", "recover",
    "error_cases.list", "error_cases.get", "error_cases.diagnose", "error_diagnoses.get",
    "error_proposals.create", "error_proposals.get", "error_proposals.validate",
    "error_rules.get", "error_rules.activate", "error_rules.deactivate",
)


def proxy_payload(*, include_parameters=True):
    """Declare only the supported Codex-to-local-proxy execution surface."""
    unsupported = {'accounts.quota.reset', 'error_cases.diagnose'}
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
        elif operation.startswith('contracts.'):
            item['limitations'] = ['schema_audit_only', 'execution_engine_is_codex']
        elif operation == 'models.list':
            item['limitations'] = ['local_catalog_is_not_provider_entitlement']
        elif operation.startswith('queues.'):
            item['limitations'] = ['conversation_local_order', 'private_context_rebind_after_dispatcher_loss',
                                   'live_provider_acceptance_pending']
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
            'routing_mode': {'support': 'adapter', 'maturity': 'fixture_tested',
                             'values': ['automatic', 'pinned'],
                             'limitations': ['between_turns_only', 'automatic_account_affinity',
                                             'provider_cache_savings_unverified']},
            'delivery': {'support': 'adapter', 'maturity': 'fixture_tested',
                         'values': ['reject', 'queue', 'steer', 'interrupt'],
                         'limitations': ['steer_requires_interactive_turn',
                                         'legacy_default_rejects_busy', 'live_provider_acceptance_pending']},
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
                                    {'value': 'dontAsk', 'display_name': 'No approval prompts'},
                                    {'value': 'default', 'display_name': 'Ask when required'},
                                ],
                                'scopes': ['instance_default', 'turn_override'],
                                'limitations': ['approvals_require_interactive_codex_transport',
                                                'approval_does_not_remove_host_isolation']},
            'sandbox_mode': {'support': 'adapter', 'maturity': 'fixture_tested',
                             'values': [
                                 {'value': 'read-only', 'display_name': 'Read only'},
                                 {'value': 'workspace-write', 'display_name': 'Workspace write'},
                                 {'value': 'danger-full-access', 'display_name': 'Full access'},
                             ],
                             'scopes': ['instance_default', 'turn_override'],
                             'limitations': ['full_access_uses_host_permissions',
                                             'full_access_incompatible_with_selected_context_or_mcp',
                                             'native_shell_requires_linux_user_namespaces',
                                             'selected_context_requires_linux_landlock',
                                             'native_process_inspection_filter_remains']},
            'allowed_tools': {'support': 'unsupported', 'maturity': 'unsupported'},
            'max_budget': {'support': 'unsupported', 'maturity': 'unsupported'},
            'provider_options': {'support': 'unsupported', 'maturity': 'unsupported'},
        }
    return value
