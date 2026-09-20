"""CLI entry point. Parsing, rendering and RPC live in separate modules."""
import json
import sys

from . import Bridge, BridgeError
from .commands.account_actions import account_command
from .commands.account_output import print_account_human
from .commands.help import PrettyHelpFormatter  # Compatibility for existing callers.
from .commands.model_actions import model_command, print_models
from .commands.parser import build_parser
from .rpc import dispatch, rpc, serial


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
    with Bridge(args.root) as bridge:
        if args.action=='rpc':rpc(bridge,sys.stdin,sys.stdout)
        elif args.action=='capabilities':print(json.dumps(bridge.capabilities(),indent=2))
        elif args.action=='models':
            try:
                print_models(model_command(bridge,args), getattr(args, 'json', False))
            except (BridgeError,ValueError,TypeError,KeyError) as error:
                print(json.dumps({'error':getattr(error,'code','invalid_params'),'message':str(error)},ensure_ascii=False),file=sys.stderr)
                raise SystemExit(1) from None
        elif args.action=='accounts':
            try:
                result=account_command(bridge,args)
                if getattr(args,'json',False):print(json.dumps(result,default=serial,indent=2,ensure_ascii=False))
                else:print_account_human(args.accounts_command,result,getattr(args,'account_name',None))
            except (BridgeError,ValueError,TypeError,KeyError) as error:
                print(json.dumps({'error':getattr(error,'code','invalid_params'),'message':str(error)},ensure_ascii=False),file=sys.stderr)
                raise SystemExit(1) from None
        else:
            try:result=dispatch(bridge,args.method,json.load(sys.stdin));print(json.dumps(result,default=serial))
            except (BridgeError,ValueError,TypeError,KeyError) as e:
                print(json.dumps({'error':getattr(e,'code','invalid_params'),'message':str(e) if isinstance(e,BridgeError) else 'Invalid parameters'}))
                raise SystemExit(1) from None


if __name__=='__main__':main()
