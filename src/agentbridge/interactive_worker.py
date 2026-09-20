"""Native duplex transports, isolated inside the supervised provider process group."""
import json
import os
import sys

from .claude_control import execute as execute_claude
from .codex_control import CodexControl
from .errors import BridgeError
from .permissions import Permissions
from .provider_channel import ProviderChannel
from .security import Redactor
from .store import Store


def main():
    emit = lambda value: print(json.dumps(value), flush=True)
    def approve(details, deliver):
        if payload['options']['permission_mode'] == 'dontAsk':
            deliver('deny')
            return
        permission_id = broker.request(payload['turn_id'], redactor.clean(details),
                                       timeout=payload['options']['timeout'])
        decision = broker.wait(payload['turn_id'], permission_id)
        deliver(decision)
        broker.delivered(payload['turn_id'], permission_id, decision)
    try:
        payload = json.load(sys.stdin)
        broker = Permissions(Store(payload['root']))
        redactor = Redactor(os.environ.get(key, '') for key in payload['secret_names'])
        with ProviderChannel(payload['command'], cwd=payload['cwd'], env=os.environ.copy()) as channel:
            if payload['engine'] == 'codex':
                CodexControl(channel, payload, emit, approve).execute()
            else:
                execute_claude(channel, payload, emit, approve)
    except BridgeError as error:
        emit({'type': 'bridge_error', 'error': error.safe_data(),
              'outcome': 'not_started' if error.phase == 'launch' else 'unknown'})
        raise SystemExit(1) from None
    except Exception:
        # Persistence and wrapper exceptions may include sensitive native data.
        # Keep the parent worker informed without printing a Python traceback.
        emit({'type': 'bridge_error', 'error': {'code': 'worker_failed'}, 'outcome': 'unknown'})
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
