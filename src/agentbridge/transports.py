"""Construct Codex proxy argv without shell evaluation."""
import json
import sys
from .errors import BridgeError, UnsupportedError
from .attachments import images
from .codex_executable import codex_argv


def require_proxy_account(account):
    if account.engine != 'codex' or not account.proxy_base_url:
        raise BridgeError('invalid_proxy_account', 'Execution requires a Codex account with a local proxy.')


def duplex(account, options):
    return account.engine == 'codex' and bool(account.proxy_base_url) and (
        bool(images(options.attachments)) or options.context_package_digest is not None
        or options.mcp_binding_digest is not None
        or options.permission_mode not in {'dontAsk', 'bypassPermissions'})


def command(account, session, options, *, native_transport=False, state_root=None):
    require_proxy_account(account)
    native=session.get('native_id')
    model=options.model or session.get('model')
    from .proxy import ProxyRoute, codex_overrides
    route_args = list(codex_overrides(ProxyRoute(account.id, account.proxy_base_url, account.key_env)))
    if options.context_window is not None:
        if type(options.context_window) is not int or options.context_window <= 0:
            raise BridgeError('invalid_context_window', 'Choose a positive context window token count.')
        route_args.extend(('-c', f'model_context_window={options.context_window}'))
    if options.permission_mode not in {'dontAsk', 'default'}:
        raise UnsupportedError('Codex supports dontAsk or default permission policy.')
    if options.max_turns is not None or options.max_budget_usd is not None or options.allowed_tools:
        raise UnsupportedError('Codex exec does not support AgentBridge turn, tool-list or dollar caps.')
    if duplex(account, options):
        return codex_argv(account, state_root) + ['app-server', '--stdio'] + route_args if native_transport else [
            sys.executable, '-P', '-m', 'agentbridge.interactive_worker']
    cmd=codex_argv(account, state_root)+['exec']
    if native:cmd+=['resume',native]
    cmd+=['--json','--skip-git-repo-check','-c','approval_policy="never"','-c',f'sandbox_mode={json.dumps(options.sandbox)}']
    cmd+=route_args
    if model:cmd+=['--model',model]
    if options.effort:cmd+=['-c',f'model_reasoning_effort={json.dumps(options.effort)}']
    return cmd+['-']
