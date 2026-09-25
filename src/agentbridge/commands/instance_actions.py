"""Create model-first conversations from the local CLI."""

import json


def create_instance(bridge, args):
    return bridge.instance_create(
        model=args.model,
        workspace_path=args.workspace_path,
        account_ref=args.account_ref,
        routing_mode=getattr(args, 'routing_mode', None),
        provider=args.provider,
        idempotency_key=args.idempotency_key,
    )


def print_instance(value, as_json=False):
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return
    print(f"Instance: {value.get('instance_id') or value.get('id')}")
    print(f"Model: {value.get('model') or '-'}")
    print(f"Account: {value.get('account_ref') or '-'}")
    if value.get('replayed'):
        print('Replayed: yes')
