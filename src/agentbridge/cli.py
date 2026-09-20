"""CLI entry point. Parsing, rendering and RPC live in separate modules."""
import json
import sys

from . import Bridge, BridgeError
from .commands.account_actions import account_command
from .commands.account_output import print_account_human
from .commands.error_actions import error_command
from .commands.contract_actions import contract_command
from .commands.help import PrettyHelpFormatter  # Compatibility for existing callers.
from .commands.model_actions import model_command, print_models
from .commands.parser import build_parser
from .commands.usage_output import print_usage, print_consumption
from .rpc import dispatch, rpc, serial


def error_payload(error):
    if isinstance(error, BridgeError):
        return {'error': error.code, 'message': str(error), 'data': error.safe_data()}
    return {'error': 'invalid_params', 'message': 'Invalid parameters.'}


def main(argv=None):
    parser, accounts = build_parser()
    args=parser.parse_args(argv)
    if args.action is None:
        parser.print_help()
        return
    if args.action == 'accounts' and args.accounts_command is None:
        accounts.print_help()
        return
    if args.action == 'models' and args.models_command is None:
        parser.parse_args(['models', '--help'])
        return
    if args.action == 'errors' and args.errors_command is None:
        parser.parse_args(['errors', '--help'])
        return
    with Bridge(args.root) as bridge:
        if args.action=='rpc':rpc(bridge,sys.stdin,sys.stdout)
        elif args.action in {'errors', 'contracts'}:
            try:
                result = (error_command if args.action == 'errors' else contract_command)(bridge, args)
                print(json.dumps(result, default=serial, indent=2, ensure_ascii=False))
            except (BridgeError, ValueError, TypeError, KeyError) as error:
                print(json.dumps(error_payload(error), ensure_ascii=False), file=sys.stderr)
                raise SystemExit(1) from None
        elif args.action=='usage':
            try:
                scope = 'account' if args.account_ref else 'instance' if args.instance_id else 'turn'
                if args.refresh and scope != 'account':
                    raise BridgeError('unsupported_parameter', 'Refresh applies to account observations only.')
                result = bridge.usage(scope, account_ref=args.account_ref, instance_id=args.instance_id,
                                      turn_id=args.turn_id, refresh=args.refresh)
                if args.json:
                    print(json.dumps(result, default=serial, indent=2, ensure_ascii=False))
                elif scope == 'account':
                    print_usage(result, args.account_ref)
                else:
                    print_consumption(result)
            except (BridgeError, ValueError, TypeError, KeyError) as error:
                print(json.dumps(error_payload(error), ensure_ascii=False), file=sys.stderr)
                raise SystemExit(1) from None
        elif args.action=='capabilities':print(json.dumps(bridge.capabilities(),indent=2))
        elif args.action=='models':
            try:
                print_models(model_command(bridge,args), getattr(args, 'json', False))
            except (BridgeError,ValueError,TypeError,KeyError) as error:
                print(json.dumps(error_payload(error),ensure_ascii=False),file=sys.stderr)
                raise SystemExit(1) from None
        elif args.action=='accounts':
            try:
                result=account_command(bridge,args)
                if getattr(args,'json',False):print(json.dumps(result,default=serial,indent=2,ensure_ascii=False))
                else:print_account_human(args.accounts_command,result,getattr(args,'account_name',None))
            except (BridgeError,ValueError,TypeError,KeyError) as error:
                print(json.dumps(error_payload(error),ensure_ascii=False),file=sys.stderr)
                raise SystemExit(1) from None
        else:
            try:result=dispatch(bridge,args.method,json.load(sys.stdin));print(json.dumps(result,default=serial))
            except (BridgeError,ValueError,TypeError,KeyError) as e:
                print(json.dumps(error_payload(e)))
                raise SystemExit(1) from None


if __name__=='__main__':main()
