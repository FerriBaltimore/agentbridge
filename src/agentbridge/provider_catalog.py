"""Read provider model catalogues without submitting a model turn."""
import json
import os
import subprocess
import sys

from .claude_control import initialize
from .credentials import environment, CURSOR_KEY_ENV
from .errors import BridgeError
from .provider_channel import ProviderChannel
from .security import base_environment


def models(account):
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
        return [dict(item, id=item.get('value') or item.get('id')) for item in values if isinstance(item, dict)]
    env['PYTHONPATH'] = os.path.dirname(os.path.dirname(__file__))
    try:
        result = subprocess.run([sys.executable, '-m', 'agentbridge.provider_catalog', account.key_env or CURSOR_KEY_ENV],
                                input='', text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise BridgeError('provider_timeout', 'The model catalog could not be read in time.') from None
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise BridgeError('provider_unavailable', 'The provider model catalog is unavailable.')
    try:
        value = json.loads(result.stdout)
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
