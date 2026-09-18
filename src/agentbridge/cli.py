"""CLI and versioned JSON-RPC 2.0 stdio surface for non-Python applications."""
import argparse
from dataclasses import asdict, is_dataclass
import json
import sys

from . import Account, Bridge, BridgeError, RunOptions, __version__


def serial(value):
    if is_dataclass(value):return asdict(value)
    raise TypeError(type(value).__name__)


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
    call=sub.add_parser('call',help='Call one API method; JSON params are read from stdin')
    call.add_argument('method')
    args=parser.parse_args(argv)
    with Bridge(args.root) as bridge:
        if args.action=='rpc':rpc(bridge,sys.stdin,sys.stdout)
        elif args.action=='capabilities':print(json.dumps(bridge.capabilities(),indent=2))
        else:
            try:result=dispatch(bridge,args.method,json.load(sys.stdin));print(json.dumps(result,default=serial))
            except (BridgeError,ValueError,TypeError,KeyError) as e:
                print(json.dumps({'error':getattr(e,'code','invalid_params'),'message':str(e) if isinstance(e,BridgeError) else 'Invalid parameters'}))
                raise SystemExit(1) from None


if __name__=='__main__':main()
