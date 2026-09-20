"""Readable account windows and observable session consumption."""
import json


def shown(value):
    return 'unknown' if value is None else str(value)


def duration(seconds):
    if seconds is None:
        return 'unknown'
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return ' '.join(f'{value}{unit}' for value, unit in
                    ((days, 'd'), (hours, 'h'), (minutes, 'm'), (seconds, 's')) if value) or '0s'


def print_usage(value, account_name=None):
    print(f"Account: {account_name or value.get('account_id') or '-'}")
    print(f"Source: {value.get('source') or 'unknown'}")
    print(f"Observed at: {value.get('observed_at') or 'unknown'}")
    print(f"Stale: {'yes' if value.get('stale', True) else 'no'}")
    for key in ('reason', 'quota_reason', 'account_usage_reason'):
        if value.get(key):
            print(f"{key.replace('_', ' ').capitalize()}: {value[key]}")
    for row in value.get('windows', []):
        print(f"Window: {row.get('id') or row['name']} ({row.get('scope', 'unknown')})")
        if row.get('label'):
            print(f"  Label: {row['label']}")
        if row.get('model_id') or row.get('model_family'):
            print(f"  Model: {row.get('model_id') or row.get('model_family')}")
        print(f"  Used: {shown(row.get('used_percent'))}% | Remaining: {shown(row.get('remaining_percent'))}%")
        print(f"  Window length: {duration(row.get('window_seconds'))}")
        print(f"  Reset at: {shown(row.get('resets_at'))} | Reset in: {duration(row.get('reset_after_seconds'))}")
        if row.get('reset_due'):
            print('  Reset time passed; refresh to verify available capacity.')
    credits = value.get('reset_credits')
    if isinstance(credits, dict):
        print(f"Reset credits: {shown(credits.get('available_count'))}")
        for row in credits.get('credits') or []:
            print(f"  {row['id']}: {shown(row.get('status'))}, expires {shown(row.get('expires_at'))}")
    details = {key: value[key] for key in ('account_usage', 'tokens', 'extra_usage', 'pools')
               if value.get(key) is not None}
    if details:
        print(json.dumps(details, indent=2, ensure_ascii=False))


def print_consumption(value):
    print(f"Scope: {value['scope']}")
    print('Observations retain their reported scope; cumulative values are not added.')
    print(json.dumps(value, indent=2, ensure_ascii=False))
