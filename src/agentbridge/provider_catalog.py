"""Read provider model catalogues without submitting a model turn."""
import json
import os
import sys

from .catalog_process import read_output
from .claude_control import initialize
from .credentials import environment, CURSOR_KEY_ENV
from .errors import BridgeError
from .provider_channel import ProviderChannel
from .security import base_environment


def models(account):
    if account.engine not in {'claude', 'cursor'}:
        raise BridgeError('unsupported_operation', 'This provider has no supported model catalog adapter.')
    env = base_environment()
    env.update(environment(account))
    if account.engine == 'claude':
        env['CLAUDE_CONFIG_DIR'] = account.home
        command = list(account.command or ('claude',)) + [
            '-p', '--output-format', 'stream-json', '--input-format', 'stream-json',
            '--verbose', '--permission-mode', 'dontAsk', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}']
        with ProviderChannel(command, cwd=account.home, env=env) as channel:
            result = initialize(channel)
        values = result.get('models')
        if not isinstance(values, list):
            raise BridgeError('provider_catalog_unsupported', 'This Claude version does not report its model catalog.')
        if any(not isinstance(item, dict) for item in values):
            raise BridgeError('provider_protocol_error', 'Claude returned malformed model catalog entries.')
        return [dict(item, id=item.get('value', item.get('id'))) for item in values]
    env['PYTHONPATH'] = os.path.dirname(os.path.dirname(__file__))
    output = read_output([sys.executable, '-P', '-m', 'agentbridge.provider_catalog', account.key_env or CURSOR_KEY_ENV],
                         env=env)
    try:
        value = json.loads(output)
        if not isinstance(value, list):
            raise ValueError()
        return value
    except ValueError:
        raise BridgeError('provider_protocol_error', 'Invalid provider model catalog.') from None


def main():
    from dataclasses import asdict
    from cursor_sdk import Cursor
    key = os.environ.get(sys.argv[1])
    if not key:
        raise SystemExit(1)
    try:
        values = Cursor.models.list(api_key=key)
        print(json.dumps([asdict(item) for item in values]))
    except Exception:
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
