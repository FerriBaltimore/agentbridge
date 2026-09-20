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
    if value.get('reason'):
        print(f"Reason: {value['reason']}")
    for model in value.get('items', value.get('models', [])):
        print(f"{model.get('id')} | {model.get('display_name') or '-'}")
        efforts = ', '.join(model.get('reasoning_efforts') or []) or 'unknown'
        default_effort = model.get('default_reasoning_effort')
        print(f"  Reasoning effort: {efforts}" + (f" (default: {default_effort})" if default_effort else ''))
        context = model.get('context_windows') or []
        print(f"  Context window: {', '.join(f'{size:,}' for size in context) + ' tokens' if context else 'unknown'}")
        for field, label in [('max_input_tokens', 'Maximum input'), ('max_output_tokens', 'Maximum output')]:
            if model.get(field) is not None:
                print(f"  {label}: {model[field]:,} tokens")
        print(f"  Input modalities: {', '.join(model.get('input_modalities') or []) or 'unknown'}")
        for parameter in model.get('parameters') or []:
            choices = ', '.join(choice['value'] for choice in parameter['values']) or 'unknown'
            print(f"  Parameter {parameter['id']}: {choices}")
    if value.get('has_more'):
        print(f"Next cursor: {value['next_cursor']}")
