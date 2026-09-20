"""Argument definitions, separate from command execution."""
import argparse

from .. import __version__
from .help import PrettyHelpFormatter, add_parser


def build_parser():
    parser=argparse.ArgumentParser(
        prog='agentbridge',
        description='Control and monitor Codex, Claude Code and Cursor with a persistent CLI.',
        epilog=(
            'Examples:\n'
            '  agentbridge accounts list\n'
            '  agentbridge accounts login --engine codex --name "Development Codex" --grantbridge-root /path/to/grantbridge\n'
            '  agentbridge accounts status "Personal Codex" --refresh\n'
            '  agentbridge accounts check "Personal Codex"'
        ),
        formatter_class=PrettyHelpFormatter,
    )
    parser.add_argument('--version',action='version',version=__version__)
    parser.add_argument('--root',default='.agentbridge',help='Private persistent state directory')
    sub=parser.add_subparsers(dest='action')
    add_parser(sub, 'capabilities',help='Show implemented capabilities per engine')
    add_parser(sub, 'rpc',help='Serve JSON-RPC 2.0 on stdin/stdout; no network listener')
    models = add_parser(sub, 'models', help='List normalized provider models')
    model_sub = models.add_subparsers(dest='models_command')
    model_list = add_parser(model_sub, 'list', help='List model catalog entries')
    model_list.add_argument('--engine', choices=('codex', 'claude', 'cursor'), required=True)
    model_list.add_argument('--account-ref')
    model_list.add_argument('--refresh', action='store_true')
    model_list.add_argument('--include-hidden', action='store_true')
    model_list.add_argument('--include-deprecated', action='store_true')
    model_list.add_argument('--limit', type=int)
    model_list.add_argument('--cursor', type=int, default=0)
    model_list.add_argument('--json', action='store_true')
    accounts=add_parser(sub, 'accounts',help='Register accounts and inspect identity, quota and usage')
    account_sub=accounts.add_subparsers(dest='accounts_command')
    add=add_parser(account_sub, 'add',help='Register an account reference; never pass a secret value')
    add.add_argument('--engine',choices=('codex','claude','cursor'),required=True)
    add.add_argument('--home',help='Native provider home for Codex or Claude')
    add.add_argument('--name',required=True,help='Unique human-facing account name')
    add.add_argument('--email')
    add.add_argument('--env',dest='env_names',action='append',default=[],help='Credential environment variable name, repeatable')
    add.add_argument('--key-env',help='API key environment variable name')
    add.add_argument('--command',nargs='+',help='Provider executable and fixed arguments')
    add.add_argument('--json',action='store_true')
    list_command=add_parser(account_sub, 'list',help='List configured accounts')
    list_command.add_argument('--json',action='store_true')
    for name, help_text in (('status','Read configured and observed authentication state'),
                            ('usage','Read account quota and token-activity observations')):
        command_parser=add_parser(account_sub, name,help=help_text)
        command_parser.add_argument('name',help='Unique account name')
        command_parser.add_argument('--refresh',action='store_true',help='Query the provider without starting a model turn')
        command_parser.add_argument('--json',action='store_true')
    history=add_parser(account_sub, 'history',help='Show stored account usage observations')
    history.add_argument('name',help='Unique account name')
    history.add_argument('--limit',type=int,default=100)
    history.add_argument('--json',action='store_true')
    check=add_parser(account_sub, 'check',help='Refresh account identity and usage together')
    check.add_argument('name',help='Unique account name')
    check.add_argument('--json',action='store_true')
    login=add_parser(account_sub, 'login', help='Authenticate or refresh a native account through GrantBridge')
    login.add_argument('--engine', choices=('codex', 'claude', 'cursor'), required=True)
    login.add_argument('--name', required=True, help='Unique human-facing account name')
    login.add_argument('--email', help='Optional configured email when the provider does not return one')
    login.add_argument('--grantbridge-root', help='GrantBridge checkout containing scripts/agentbridge-adapter.mjs')
    login.add_argument('--grantbridge-data-dir', help='GrantBridge data directory; credentials remain owned by GrantBridge')
    login.add_argument('--mode', choices=('browser', 'device', 'hosted'), default='browser', help='GrantBridge provider login mode')
    login.add_argument('--browser', choices=('same_host', 'remote_desktop', 'mobile', 'mobile_vm'), default='same_host', help='Where the provider login is completed')
    login.add_argument('--timeout', type=float, default=600, help='Maximum login wait in seconds')
    login.add_argument('--poll-interval', type=float, default=1.0, help='Status polling interval in seconds')
    login.add_argument('--json', action='store_true')
    login.add_argument('--inference', action='store_true', help='Authorize a small verification model turn (may incur usage)')
    start = add_parser(account_sub, 'login-start', help='Start asynchronous GrantBridge authentication')
    start.add_argument('--engine', choices=('codex', 'claude', 'cursor'), required=True)
    start.add_argument('--name', required=True)
    start.add_argument('--email')
    start.add_argument('--grantbridge-root')
    start.add_argument('--grantbridge-data-dir')
    start.add_argument('--mode', choices=('browser', 'device', 'hosted'), default='browser')
    start.add_argument('--browser', choices=('same_host', 'remote_desktop', 'mobile', 'mobile_vm'), default='same_host')
    start.add_argument('--request-key')
    start.add_argument('--owner-ref')
    start.add_argument('--json', action='store_true')
    for name, help_text in (('login-status', 'Read a GrantBridge authentication attempt'),
                            ('login-check', 'Verify an authentication attempt in a fresh provider process'),
                            ('login-complete', 'Bind a verified authentication attempt as an account'),
                            ('login-cancel', 'Cancel a GrantBridge authentication attempt')):
        auth_command = add_parser(account_sub, name, help=help_text)
        auth_command.add_argument('attempt_id')
        auth_command.add_argument('--owner-ref', help='Opaque owner reference returned by login-start')
        auth_command.add_argument('--account-ref', help='Existing AgentBridge account reference')
        auth_command.add_argument('--grantbridge-root')
        auth_command.add_argument('--grantbridge-data-dir')
        auth_command.add_argument('--json', action='store_true')
        if name == 'login-check':
            auth_command.add_argument('--inference', action='store_true', help='Authorize a small verification model turn (may incur usage)')
    call=add_parser(sub, 'call',help='Call one API method; JSON params are read from stdin')
    call.add_argument('method')
    return parser, accounts
