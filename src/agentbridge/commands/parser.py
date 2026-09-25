"""Argument definitions, separate from command execution."""
import argparse

from .. import __version__
from .error_actions import add_errors
from .contract_actions import add_contracts
from .queue_actions import add_queues
from .help import PrettyHelpFormatter, add_parser


def build_parser():
    parser=argparse.ArgumentParser(
        prog='agentbridge',
        description='Run coding models with managed accounts and persistent conversations.',
        epilog=(
            'Examples:\n'
            '  agentbridge models list\n'
            '  agentbridge instances create --model MODEL --workspace-path /path/to/workspace\n'
            '  agentbridge accounts list\n'
            '  agentbridge accounts login --name Primary --provider codex\n'
            '  agentbridge accounts status "Personal Codex" --refresh\n'
            '  agentbridge accounts check "Personal Codex"'
        ),
        formatter_class=PrettyHelpFormatter,
    )
    parser.add_argument('--version',action='version',version=__version__)
    parser.add_argument('--root', help='Private persistent state directory (default: XDG state)')
    sub=parser.add_subparsers(dest='action')
    add_errors(sub)
    add_contracts(sub)
    add_queues(sub)
    add_parser(sub, 'capabilities',help='Show implemented capabilities')
    add_parser(sub, 'rpc',help='Serve JSON-RPC 2.0 on stdin/stdout; no network listener')
    models = add_parser(sub, 'models', help='List available models')
    model_sub = models.add_subparsers(dest='models_command')
    model_list = add_parser(model_sub, 'list', help='List model catalog entries')
    model_list.add_argument('--account-ref')
    model_list.add_argument('--refresh', action='store_true')
    model_list.add_argument('--include-hidden', action='store_true')
    model_list.add_argument('--include-deprecated', action='store_true')
    model_list.add_argument('--limit', type=int)
    model_list.add_argument('--cursor', type=int, default=0)
    model_list.add_argument('--json', action='store_true')
    instances = add_parser(sub, 'instances', help='Create durable coding conversations')
    instance_sub = instances.add_subparsers(dest='instances_command')
    create = add_parser(instance_sub, 'create', help='Create a conversation for a selected model')
    create.add_argument('--model', required=True, help='Model shown by models list')
    create.add_argument('--workspace-path', default='.', help='Existing workspace directory')
    create.add_argument('--account-ref', help='Select a configured account; pinned unless routing mode is automatic')
    create.add_argument('--routing-mode', choices=('automatic', 'pinned'),
                        help='Keep account affinity automatically or pin the selected account')
    create.add_argument('--provider', choices=('codex', 'claude', 'grok'),
                        help='Limit automatic routing to accounts for this provider')
    create.add_argument('--idempotency-key', help='Reuse this key to reconcile a lost response')
    create.add_argument('--json', action='store_true')
    update = add_parser(instance_sub, 'update', help='Update conversation routing or execution defaults')
    update.add_argument('instance_id')
    update.add_argument('--model')
    update.add_argument('--account-ref')
    update.add_argument('--routing-mode', choices=('automatic', 'pinned'))
    update.add_argument('--provider', choices=('codex', 'claude', 'grok'))
    update.add_argument('--expected-version', type=int)
    update.add_argument('--json', action='store_true')
    for command_parser in (create, update):
        command_parser.add_argument('--permission-mode', choices=('dontAsk', 'default'),
                                    help='Default tool approval policy for this conversation')
        command_parser.add_argument('--sandbox-mode',
            choices=('read-only', 'workspace-write', 'danger-full-access'),
            help='Default filesystem access for this conversation')
    accounts=add_parser(sub, 'accounts',help='Authenticate accounts and inspect identity, quota and usage')
    account_sub=accounts.add_subparsers(dest='accounts_command')
    list_command=add_parser(account_sub, 'list',help='List configured accounts')
    list_command.add_argument('--json',action='store_true')
    delete=add_parser(account_sub, 'delete',help='Remove a local account route and retain history')
    delete.add_argument('name', nargs='?', help='Account name or reference')
    delete.add_argument('--account-id', help='Exact account ID for a retirement retry')
    delete.add_argument('--json',action='store_true')
    for name, help_text in (('pause', 'Pause account routing for new work'),
                            ('resume', 'Resume account routing for new work')):
        command_parser = add_parser(account_sub, name, help=help_text)
        command_parser.add_argument('name', help='Account name or reference')
        command_parser.add_argument('--json', action='store_true')
    for name, help_text in (('status','Read configured and observed authentication state'),
                            ('usage','Read account quota and token-activity observations')):
        command_parser=add_parser(account_sub, name,help=help_text)
        command_parser.add_argument('name',help='Unique account name')
        command_parser.add_argument('--refresh',action='store_true',help='Read the local proxy without starting a model turn')
        command_parser.add_argument('--json',action='store_true')
    history=add_parser(account_sub, 'history',help='Show stored account usage observations')
    history.add_argument('name',help='Unique account name')
    history.add_argument('--limit',type=int,default=100)
    history.add_argument('--json',action='store_true')
    check=add_parser(account_sub, 'check',help='Refresh account identity and usage together')
    check.add_argument('name',help='Unique account name')
    check.add_argument('--json',action='store_true')
    login=add_parser(account_sub, 'login', help='Authenticate and configure a proxy account through GrantBridge')
    _add_login_account_options(login)
    login.add_argument('--email', help='Optional configured email when the provider does not return one')
    login.add_argument('--grantbridge-root', help='GrantBridge checkout containing scripts/agentbridge-adapter.mjs')
    login.add_argument('--grantbridge-data-dir', help='GrantBridge adapter data directory; OAuth credentials remain in the local proxy')
    login.add_argument('--mode', choices=('browser',), default='browser',
                       help='Browser login required for local proxy binding')
    login.add_argument('--browser', choices=('same_host',), default='same_host',
                       help='Complete login on the same host as the local proxy')
    login.add_argument('--timeout', type=float, default=600, help='Maximum login wait in seconds')
    login.add_argument('--poll-interval', type=float, default=1.0, help='Status polling interval in seconds')
    login.add_argument('--json', action='store_true')
    start = add_parser(account_sub, 'login-start', help='Start asynchronous proxy account authentication')
    _add_login_account_options(start)
    start.add_argument('--email')
    start.add_argument('--grantbridge-root')
    start.add_argument('--grantbridge-data-dir')
    start.add_argument('--mode', choices=('browser',), default='browser',
                       help='Browser login required for local proxy binding')
    start.add_argument('--browser', choices=('same_host',), default='same_host',
                       help='Complete login on the same host as the local proxy')
    start.add_argument('--request-key')
    start.add_argument('--owner-ref')
    start.add_argument('--json', action='store_true')
    for name, help_text in (('login-status', 'Read a proxy authentication attempt'),
                            ('login-check', 'Verify proxy identity and models through the local management API'),
                            ('login-complete', 'Bind a verified authentication attempt as an account'),
                            ('login-cancel', 'Cancel or close a proxy authentication attempt')):
        auth_command = add_parser(account_sub, name, help=help_text)
        auth_command.add_argument('attempt_id')
        auth_command.add_argument('--owner-ref', help='Opaque owner reference returned by login-start')
        auth_command.add_argument('--account-ref', help='Existing AgentBridge account reference')
        auth_command.add_argument('--grantbridge-root')
        auth_command.add_argument('--grantbridge-data-dir')
        auth_command.add_argument('--json', action='store_true')
    call=add_parser(sub, 'call',help='Call one API method; JSON params are read from stdin')
    call.add_argument('method')
    usage=add_parser(sub, 'usage', help='Read account, instance or turn consumption observations')
    scopes=usage.add_mutually_exclusive_group(required=True)
    scopes.add_argument('--account-ref')
    scopes.add_argument('--instance-id')
    scopes.add_argument('--turn-id')
    usage.add_argument('--refresh', action='store_true', help='Refresh account observations only')
    usage.add_argument('--json', action='store_true')
    return parser, accounts


def _add_login_account_options(parser):
    parser.add_argument('--provider', choices=('codex', 'claude', 'grok'), required=True,
                        help='Provider account authenticated through the local proxy')
    parser.add_argument('--name', required=True, help='Unique human-facing account name')
    parser.add_argument('--proxy-base-url',
                        help='Advanced: existing loopback CLIProxyAPI /v1 endpoint')
    parser.add_argument('--proxy-key-env', dest='key_env',
                        help='Advanced: existing proxy client key environment variable')
    parser.add_argument('--proxy-management-key-env', dest='management_key_env',
                        help='Advanced: existing proxy management key environment variable')
