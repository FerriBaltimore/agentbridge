"""Optional Cursor SDK subprocess; no Fullbrain imports or provider key in argv."""
from dataclasses import asdict, is_dataclass
import json
import os
import signal
import sys

from .provider_errors import exception, normalize
from .errors import BridgeError
from .cursor_options import model_selection


def plain(value):
    if is_dataclass(value):return plain(asdict(value))
    if isinstance(value,dict):return {k:plain(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [plain(v) for v in value]
    if value is None or isinstance(value,(str,int,float,bool)):return value
    return None


def execute(payload, emit, sdk=None):
    if sdk is None:
        import cursor_sdk as sdk
    api_key = os.environ.get(payload.get('key_env') or '')
    if not api_key:
        raise ValueError('An explicit Cursor credential is required.')
    kwargs={'cwd':payload['cwd'],'setting_sources':[]}
    # Cursor exposes its own sandbox controls. A read-only request uses an empty
    # tool set, so we never claim an OS read-only sandbox that the SDK cannot express.
    model = model_selection(payload['model'], payload.get('effort'), sdk=sdk, api_key=api_key)
    options=sdk.AgentOptions(model=model,api_key=api_key,
        local=sdk.LocalAgentOptions(**kwargs),
        tools=[] if payload.get('sandbox','read-only')=='read-only' else payload.get('tools') or None)
    agent=sdk.Agent.resume(payload['native_id'],options) if payload.get('native_id') else sdk.Agent.create(options)
    active=[None]
    def cancel(*_):
        if active[0] is not None:
            try:active[0].cancel()
            except Exception:pass
    if __name__=='__main__':signal.signal(signal.SIGTERM,cancel)
    with agent:
        pictures = [{'data': item['data'], 'mime_type': item['media_type']}
                    for item in payload.get('attachments', []) if item['type'] == 'image']
        message = {'text': payload['prompt'], 'images': pictures} if pictures else payload['prompt']
        run=agent.send(message);active[0]=run
        emit({'type':'bridge_session','native_id':agent.agent_id})
        for message in run.messages():
            value=plain(message)
            if not isinstance(value,dict):
                emit({'type':'unsupported_message'});continue
            kind=value.get('type')
            if kind=='thinking':continue
            if kind == 'status' and value.get('status') in {'error', 'failed'}:
                emit({'type': 'bridge_error', 'error': normalize('cursor', value)})
                continue
            allowed={'type','agent_id','run_id','subtype','message','call_id','name','status','args','result',
                     'truncated','task_id','text','request_id','usage'}
            emit({k:v for k,v in value.items() if k in allowed})
        result=run.wait()
        run_usage = plain(getattr(result, 'usage', None))
        if isinstance(run_usage, dict) and run_usage:
            emit({'type': 'bridge_usage', 'scope': 'turn', 'usage': run_usage})
        if payload.get('collect_usage'):
            try:
                # Local usage is session cumulative; do not mislabel it as this run's delta.
                usage=plain(agent.get_usage()) or {}
                if usage.get('usage') is not None or usage.get('cost') is not None:
                    emit({'type':'bridge_usage','scope':'session','usage':usage.get('usage'),
                          'cost':usage.get('cost'), 'aggregation': 'cumulative'})
                else:
                    emit({'type':'status','status':'usage_unavailable'})
            except Exception:
                emit({'type':'status','status':'usage_unavailable'})
        status = getattr(result, 'status', 'unknown')
        event = {'type': 'bridge_result', 'status': status}
        if status == 'error':
            event['error'] = normalize('cursor', {'message': getattr(result, 'result', '')})
        emit(event)


def main():
    emit=lambda event:print(json.dumps(event),flush=True)
    try:execute(json.load(sys.stdin),emit)
    except BridgeError as error:
        emit({'type': 'bridge_error', 'error': error.safe_data(), 'outcome': error.outcome})
        raise SystemExit(1) from None
    except Exception as error:
        issue = normalize('cursor', {'code': 'cursor_sdk_unavailable'}) if isinstance(error, ImportError) else exception('cursor', error)
        emit({'type': 'bridge_error', 'error': issue})
        raise SystemExit(1) from None


if __name__=='__main__':main()
