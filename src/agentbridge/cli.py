"""CLI and versioned JSON-RPC 2.0 stdio surface for non-Python applications."""
import argparse
from dataclasses import asdict, is_dataclass
import json
import sys

from . import Account, Bridge, BridgeError, RunOptions, __version__


def serial(value):
    if is_dataclass(value):return asdict(value)
    raise TypeError(type(value).__name__)


def _account_dict(account):
    return asdict(account) if is_dataclass(account) else account


def _account_command(bridge, args):
    command = args.accounts_command
    if command == 'add':
        account = Account(id=args.id, engine=args.engine, home=args.home, name=args.name,
                          email=args.email, env_names=tuple(args.env_names or ()),
                          key_env=args.key_env, command=tuple(args.command or ()))
        return _account_dict(bridge.register(account))
    if command == 'list':
        return [_account_dict(account) for account in bridge.accounts()]
    if command == 'status':
        return bridge.account_status(args.id, refresh=args.refresh)
    if command == 'usage':
        return bridge.account_usage(args.id, refresh=args.refresh)
    if command == 'history':
        return bridge.account_usage_history(args.id, limit=args.limit)
    if command == 'check':
        return {'status': bridge.account_status(args.id, refresh=True),
                'usage': bridge.account_usage(args.id, refresh=True)}
    raise BridgeError('invalid_command', 'Unknown accounts command.')


def _print_account_human(command, value):
    if command == 'list':
        if not value:
            print('No hay cuentas registradas.')
            return
        print('ID | Motor | Nombre | Correo')
        print('---|---|---|---')
        for account in value:
            print(' | '.join(str(account.get(key) or '-') for key in ('id', 'engine', 'name', 'email')))
        return
    if command == 'add':
        print(f"Cuenta añadida: {value['id']} ({value['engine']})")
        return
    if command == 'status':
        auth = value.get('authentication', {})
        identity = value.get('identity') or {}
        configured = value.get('configured') or {}
        print(f"Cuenta: {value.get('account_id')}")
        print(f"Motor: {configured.get('engine') or '-'}")
        print(f"Nombre configurado: {configured.get('name') or '-'}")
        print(f"Correo configurado: {configured.get('email') or '-'}")
        print(f"Autenticación: {auth.get('status') or '-'}")
        print(f"Identidad observada: {identity.get('email') or identity.get('type') or '-'}")
        print(f"Observada: {auth.get('observed_at') or '-'}")
        if value.get('reason'):
            print(f"Motivo: {value['reason']}")
        return
    if command == 'usage':
        print(f"Cuenta: {value.get('account_id')}")
        print(f"Fuente: {value.get('source') or '-'}")
        print(f"Observado: {value.get('observed_at') or '-'}")
        print(f"Obsoleto: {'sí' if value.get('stale') else 'no'}")
        if value.get('reason'):
            print(f"Motivo: {value['reason']}")
        details = {key: value[key] for key in ('quota', 'account_usage') if key in value}
        if details:
            print(json.dumps(details, indent=2, ensure_ascii=False))
        return
    print(json.dumps(value, indent=2, ensure_ascii=False))


def dispatch(bridge,method,params):
    if method=='capabilities':return bridge.capabilities(**params)
    if method=='accounts.list':return [asdict(x) for x in bridge.accounts()]
    if method=='accounts.register':return asdict(bridge.register(Account(**params)))
    if method=='accounts.status':return bridge.account_status(**params)
    if method=='accounts.usage':return bridge.account_usage(**params)
    if method=='accounts.usage_history':return bridge.account_usage_history(**params)
    if method=='accounts.quota':return bridge.quota(**params)
    if method=='sessions.create':return bridge.session(**params)
    if method=='sessions.list':return bridge.sessions()
    if method=='sessions.get':return bridge.get_session(**params)
    if method=='sessions.transfer':return bridge.transfer(**params)
    if method=='sessions.export':return asdict(bridge.export_context(**params))
    if method=='runs.submit':
        p=dict(params);p['options']=RunOptions(**p.get('options',{}))
        return {'run_id':bridge.submit(**p).id}
    if method=='runs.list':return bridge.runs()
    if method=='recover':return bridge.recover()
    if method.startswith('runs.'):
        p=dict(params);run=bridge.run(p.pop('run_id'))
        if method=='runs.get':return run.snapshot
        if method=='runs.events':return [asdict(x) for x in run.events(after=p.get('after',0))]
        if method=='runs.stop':return run.stop()
        if method=='runs.usage':return run.consumption
        if method=='runs.subagents':return run.subagents
        if method=='runs.resume':
            if 'options' in p:p['options']=RunOptions(**p['options'])
            return {'run_id':run.resume(**p).id}
    raise BridgeError('method_not_found','Unknown method.')


def rpc(bridge,inp,out):
    for line in inp:
        id=None;notification=False
        try:
            request=json.loads(line)
            if not isinstance(request,dict) or request.get('jsonrpc')!='2.0' or not isinstance(request.get('method'),str):
                raise BridgeError('invalid_request','Expected a JSON-RPC 2.0 request.')
            id=request.get('id');notification='id' not in request
            if not isinstance(request.get('params',{}),dict):raise BridgeError('invalid_params','params must be an object.')
            result=dispatch(bridge,request['method'],request.get('params',{}))
            response={'jsonrpc':'2.0','id':id,'result':result}
        except json.JSONDecodeError:response={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Invalid JSON'}}
        except BridgeError as e:
            codes={'method_not_found':-32601,'invalid_request':-32600,'invalid_params':-32602}
            response={'jsonrpc':'2.0','id':id,'error':{'code':codes.get(e.code,-32000),'message':str(e),'data':{'code':e.code}}}
        except (ValueError,TypeError,KeyError):response={'jsonrpc':'2.0','id':id,'error':{'code':-32602,'message':'Invalid parameters'}}
        except Exception:response={'jsonrpc':'2.0','id':id,'error':{'code':-32603,'message':'Internal error'}}
        if not notification:out.write(json.dumps(response,default=serial)+'\n');out.flush()


def main(argv=None):
    parser=argparse.ArgumentParser(prog='agentbridge',description='Control coding agents through a persistent SDK and JSON API.')
    parser.add_argument('--version',action='version',version=__version__)
    parser.add_argument('--root',default='.agentbridge',help='Private persistent state directory')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('capabilities',help='Show implemented capabilities per engine')
    sub.add_parser('rpc',help='Serve JSON-RPC 2.0 on stdin/stdout; no network listener')
    accounts=sub.add_parser('accounts',help='Register accounts and inspect identity, quota and usage')
    account_sub=accounts.add_subparsers(dest='accounts_command',required=True)
    add=account_sub.add_parser('add',help='Register an account reference; never pass a secret value')
    add.add_argument('id',help='Stable local account ID')
    add.add_argument('--engine',choices=('codex','claude','cursor'),required=True)
    add.add_argument('--home',help='Native provider home for Codex or Claude')
    add.add_argument('--name')
    add.add_argument('--email')
    add.add_argument('--env',dest='env_names',action='append',default=[],help='Credential environment variable name, repeatable')
    add.add_argument('--key-env',help='API key environment variable name')
    add.add_argument('--command',nargs='+',help='Provider executable and fixed arguments')
    add.add_argument('--json',action='store_true')
    list_command=account_sub.add_parser('list',help='List configured accounts')
    list_command.add_argument('--json',action='store_true')
    for name, help_text in (('status','Read configured and observed authentication state'),
                            ('usage','Read account quota and token-activity observations')):
        command_parser=account_sub.add_parser(name,help=help_text)
        command_parser.add_argument('id')
        command_parser.add_argument('--refresh',action='store_true',help='Query the provider without starting a model turn')
        command_parser.add_argument('--json',action='store_true')
    history=account_sub.add_parser('history',help='Show stored account usage observations')
    history.add_argument('id')
    history.add_argument('--limit',type=int,default=100)
    history.add_argument('--json',action='store_true')
    check=account_sub.add_parser('check',help='Refresh account identity and usage together')
    check.add_argument('id')
    check.add_argument('--json',action='store_true')
    call=sub.add_parser('call',help='Call one API method; JSON params are read from stdin')
    call.add_argument('method')
    args=parser.parse_args(argv)
    with Bridge(args.root) as bridge:
        if args.action=='rpc':rpc(bridge,sys.stdin,sys.stdout)
        elif args.action=='capabilities':print(json.dumps(bridge.capabilities(),indent=2))
        elif args.action=='accounts':
            try:
                result=_account_command(bridge,args)
                if getattr(args,'json',False):print(json.dumps(result,default=serial,indent=2,ensure_ascii=False))
                else:_print_account_human(args.accounts_command,result)
            except (BridgeError,ValueError,TypeError,KeyError) as error:
                print(json.dumps({'error':getattr(error,'code','invalid_params'),'message':str(error)},ensure_ascii=False),file=sys.stderr)
                raise SystemExit(1) from None
        else:
            try:result=dispatch(bridge,args.method,json.load(sys.stdin));print(json.dumps(result,default=serial))
            except (BridgeError,ValueError,TypeError,KeyError) as e:
                print(json.dumps({'error':getattr(e,'code','invalid_params'),'message':str(e) if isinstance(e,BridgeError) else 'Invalid parameters'}))
                raise SystemExit(1) from None


if __name__=='__main__':main()
