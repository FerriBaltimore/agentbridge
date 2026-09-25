"""Submit one deterministic acceptance turn from a fresh SDK process."""

import json
import sys

from agentbridge import Bridge


def main():
    request = json.load(sys.stdin)
    bridge = Bridge(request['state'])
    try:
        accepted = bridge.message_create(
            request['instance_id'], request['prompt'],
            permission_mode=request['permission_mode'], timeout_ms=30000)
        run = bridge.run(accepted['turn_id'])
        result = run.wait(40)
        print(json.dumps({
            'state': result['state'], 'error': result['error'],
            'text': run.text, 'native_id': bridge.instance_get(request['instance_id'])['native_session_id'],
            'child_pid': result['child_pid'],
        }))
    finally:
        bridge.close(cancel=True)


if __name__ == '__main__':
    main()
