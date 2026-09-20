"""Observable native retry and model-routing notices, without provider prose."""
import re

from .provider_errors import normalize


def identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}', value) else None


def routing(engine, event):
    if engine == 'codex':
        return {'engine': engine, 'source': 'provider', 'scope': 'turn',
                'from_model': identifier(event.get('fromModel')),
                'to_model': identifier(event.get('toModel')),
                'reason': 'safety_blocked' if event.get('reason') == 'highRiskCyberActivity' else 'unknown',
                'direction': 'reroute', 'host_initiated': False}
    value = {'engine': engine, 'source': 'provider',
             'scope': ('session' if 'scope' not in event else event['scope']
                       if event['scope'] in {'session', 'local'} else 'unknown'),
             'from_model': identifier(event.get('original_model')),
             'to_model': identifier(event.get('fallback_model')), 'reason': 'safety_blocked',
             'direction': event.get('direction') if event.get('direction') in {'retry', 'revert', 'sticky'} else 'unknown',
             'host_initiated': False}
    ids = event.get('retracted_message_uuids')
    if isinstance(ids, list):
        value['retracted_provider_message_ids'] = [item for item in ids[:1000] if identifier(item)]
    return value


def retry(engine, event):
    issue = normalize(engine, {'code': event.get('error'), 'status': event.get('error_status')},
                      terminal=False, provider_retrying=True)
    value = {'engine': engine, 'source': 'provider', 'host_initiated': False, 'error': issue}
    for key in ('attempt', 'max_retries', 'retry_delay_ms'):
        number = event.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 2**31 - 1:
            value[key] = number
    return value
