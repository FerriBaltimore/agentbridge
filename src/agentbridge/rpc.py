"""JSON-RPC dispatch and stdio framing for the existing public contract."""
from dataclasses import asdict, is_dataclass
import json

from . import Account, BridgeError, RunOptions


def serial(value):
    if is_dataclass(value):return asdict(value)
    raise TypeError(type(value).__name__)


def public_account(account, status=None, account_ref=None):
    value = asdict(account) if is_dataclass(account) else dict(account)
    result = {
        'account_ref': account_ref or value.get('name') or value.get('id') or value.get('account_ref'),
        'name': value.get('name'),
        'email': value.get('email'),
    }
    if value.get('provider'):
        result['provider'] = value['provider']
    if value.get('supported_models'):
        result['supported_models'] = list(value['supported_models'])
    if status:
        result['authentication'] = status.get('authentication', {})
        result['routing'] = status.get('routing', {'paused': False, 'paused_at': None})
        result['identity'] = status.get('identity', {})
        if status.get('reason'):
            result['reason'] = status['reason']
    return result


def public_login(result, bridge=None):
    value = dict(result)
    if isinstance(value.get('account'), dict):
        account = value['account']
        reference = bridge.account_reference(account['id']) if bridge else None
        value['account'] = public_account(account, account_ref=reference)
    value.pop('home', None)
    return value


def dispatch(bridge,method,params):
    contract_methods = {'contracts.list': 'provider_contracts', 'contracts.get': 'provider_contract',
                        'contracts.check': 'provider_compatibility', 'contracts.inspect': 'provider_inspect'}
    if method in contract_methods:
        return getattr(bridge, contract_methods[method])(**params)
    error_methods = {
        'error_cases.list': 'error_cases', 'error_cases.get': 'error_case',
        'error_cases.diagnose': 'error_diagnose', 'error_diagnoses.get': 'error_diagnosis',
        'error_proposals.create': 'error_propose', 'error_proposals.get': 'error_proposal',
        'error_proposals.validate': 'error_validate', 'error_rules.get': 'error_rule',
        'error_rules.activate': 'error_activate', 'error_rules.deactivate': 'error_deactivate',
    }
    if method in error_methods:
        return getattr(bridge, error_methods[method])(**params)
    if method.startswith('accounts.login') and set(params) & {'client', 'grantbridge_root', 'data_dir', 'on_attempt'}:
        raise BridgeError('unsupported_parameter', 'Authentication transport configuration belongs to the local host.')
    if method=='capabilities':return bridge.capabilities(**params)
    if method=='capabilities.get':return bridge.capabilities(**params)
    if method=='accounts.list':
        accounts = bridge.accounts(**params)
        return [public_account(account, bridge.account_status(account_id=account.id),
                               account_ref=bridge.account_reference(account.id))
                for account in accounts]
    if method=='accounts.register':
        raise BridgeError('authentication_required', 'Create accounts through the proxy login flow.')
    if method=='accounts.delete':
        return bridge.account_delete(**params)
    if method=='accounts.pause':
        return bridge.account_pause(**params)
    if method=='accounts.resume':
        return bridge.account_resume(**params)
    if method=='accounts.status':
        return bridge.account_status(**params)
    if method=='accounts.usage':
        value = dict(params)
        value['account_id'] = value.pop('account_ref', value.pop('account_id', None))
        return bridge.account_usage(**value)
    if method=='accounts.reset_credits':
        return bridge.account_reset_credits(**params)
    if method=='accounts.quota.reset':
        return bridge.account_quota_reset(**params)
    if method=='accounts.usage_history':
        value = dict(params)
        value['account_id'] = value.pop('account_ref', value.pop('account_id', None))
        return bridge.account_usage_history(**value)
    if method=='accounts.quota':
        value = dict(params)
        value['account_id'] = value.pop('account_ref', value.pop('account_id', None))
        return bridge.quota(**value)
    if method=='accounts.login':
        value = dict(params)
        if 'timeout_ms' in value:
            value['timeout'] = value.pop('timeout_ms') / 1000
        if 'poll_interval_ms' in value:
            value['poll_interval'] = value.pop('poll_interval_ms') / 1000
        return public_login(bridge.account_login(**value), bridge)
    if method=='accounts.login.list':
        return bridge.account_login_attempts(**params)
    if method in ('accounts.login.start', 'accounts.login_start'):
        return bridge.account_login_start(**params)
    if method in ('accounts.login.status', 'accounts.login_status'):
        return bridge.account_login_status(**params)
    if method in ('accounts.login.check', 'accounts.login_check'):
        return bridge.account_login_check(**params)
    if method in ('accounts.login.complete', 'accounts.login_complete'):
        return public_login(bridge.account_login_complete(**params), bridge)
    if method in ('accounts.login.cancel', 'accounts.login_cancel'):
        return bridge.account_login_cancel(**params)
    if method=='accounts.login.callback':
        return bridge.account_login_callback(**params)
    if method=='usage.get':
        value = dict(params)
        value['account_ref'] = value.pop('account_ref', value.pop('account_id', None))
        return bridge.usage(**value)
    if method=='usage.history':
        value = dict(params)
        account = value.pop('account_ref', value.pop('account_id', None))
        return bridge.account_usage_history(account, **value)
    if method=='models.list':return bridge.models(**params)
    if method=='instances.create':return bridge.instance_create(**params)
    if method=='instances.get':return bridge.instance_get(**params)
    if method=='instances.list':return bridge.instances(**params)
    if method=='instances.update':return bridge.instance_update(**params)
    if method=='instances.archive':return bridge.instance_archive(**params)
    if method=='instances.delete':return bridge.instance_delete(**params)
    if method=='instances.discard_evaluation':return bridge.instance_discard_evaluation(**params)
    if method=='instances.events':return bridge.instance_events(**params)
    if method=='instances.export':
        value = dict(params)
        value['instance_id'] = value.pop('instance_id', value.pop('session_id', None))
        return asdict(bridge.instance_export(**value))
    if method=='messages.create':return bridge.message_create(**params)
    if method=='messages.list':return bridge.messages(**params)
    if method=='turns.list':return bridge.turns(**params)
    if method=='turns.get':return bridge.turn(**params)
    if method=='turns.events':return bridge.turn_events(**params)
    if method=='turns.stop':return bridge.turn_stop(**params)
    if method=='turns.resume':return bridge.turn_resume(**params)
    if method=='permissions.respond':return bridge.permission_respond(**params)
    if method=='instances.transfer':
        value = dict(params)
        value['session_id'] = value.pop('instance_id')
        value['account_id'] = value.pop('target_account_ref')
        return bridge.transfer(**value)
    if method=='sessions.create':return bridge.session(**params)
    if method=='sessions.list':return bridge.sessions()
    if method=='sessions.get':return bridge.get_session(**params)
    if method=='sessions.transfer':return bridge.transfer(**params)
    if method=='sessions.export':return asdict(bridge.export_context(**params))
    if method=='runs.submit':
        p=dict(params);p['options']=RunOptions(**p.get('options',{}))
        return {'run_id':bridge.submit(**p).id}
    if method=='runs.list':return bridge.runs()
    if method=='recover':return bridge.recover(**params)
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
        except json.JSONDecodeError:
            error = BridgeError('invalid_json', 'Invalid JSON.', phase='admission', outcome='not_started', retryable=False)
            response={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':str(error),
                      'data':error.safe_data()}}
        except BridgeError as e:
            codes={'method_not_found':-32601,'invalid_request':-32600,'invalid_params':-32602}
            response={'jsonrpc':'2.0','id':id,'error':{'code':codes.get(e.code,-32000),'message':str(e),'data':e.safe_data()}}
        except TimeoutError:
            error = BridgeError('provider_timeout', 'The provider did not finish before the deadline.',
                                phase='execution', outcome='unknown', retryable=False)
            response={'jsonrpc':'2.0','id':id,'error':{'code':-32000,'message':str(error),
                      'data':error.safe_data()}}
        except (ValueError,TypeError,KeyError) as error:
            invalid = BridgeError('invalid_params', 'Invalid parameters.',
                                  details={'error_type': type(error).__name__})
            response={'jsonrpc':'2.0','id':id,'error':{'code':-32602,'message':str(invalid),
                      'data':invalid.safe_data()}}
        except Exception:
            error = BridgeError('internal_error', 'Internal error.', phase='dispatch', outcome='unknown', retryable=False)
            response={'jsonrpc':'2.0','id':id,'error':{'code':-32603,'message':str(error),
                      'data':error.safe_data()}}
        if not notification:out.write(json.dumps(response,default=serial)+'\n');out.flush()
