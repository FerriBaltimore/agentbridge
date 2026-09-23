"""Provider contract inspection commands; no runtime approval or auto-upgrade."""
from .help import add_parser


def add_contracts(sub):
    parser = add_parser(sub, 'contracts', help='Inspect release schemas; Claude records are historical')
    actions = parser.add_subparsers(dest='contracts_command', required=True)
    listing = add_parser(actions, 'list', help='List shared dialects and exact release bindings')
    listing.add_argument('--engine', choices=('codex', 'claude'))
    get = add_parser(actions, 'get', help='Read an immutable contract by content hash')
    get.add_argument('contract_id')
    check = add_parser(actions, 'check', help='Check the installed release for a configured account')
    check.add_argument('account_ref')
    inspect = add_parser(actions, 'inspect', help='Audit installed schemas offline; does not enable an execution engine')
    inspect.add_argument('--engine', choices=('codex', 'claude'), required=True)


def contract_command(bridge, args):
    if args.contracts_command == 'list':
        return bridge.provider_contracts(args.engine)
    if args.contracts_command == 'get':
        return bridge.provider_contract(args.contract_id)
    if args.contracts_command == 'check':
        return bridge.provider_compatibility(args.account_ref)
    return bridge.provider_inspect(args.engine)
