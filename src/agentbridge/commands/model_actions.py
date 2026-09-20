"""Execute and render model catalog CLI commands."""
import json


def model_command(bridge, args):
    return bridge.models(
        args.engine, account_ref=args.account_ref, refresh=args.refresh,
        include_hidden=args.include_hidden, include_deprecated=args.include_deprecated,
        limit=args.limit, cursor=args.cursor)


def print_models(value, as_json=False):
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return
    print(f"Engine: {value.get('engine')}")
    print(f"Source: {value.get('source')}")
    print(f"Stale: {'yes' if value.get('stale') else 'no'}")
    for model in value.get('items', value.get('models', [])):
        print(f"{model.get('id')} | {model.get('display_name') or '-'}")
