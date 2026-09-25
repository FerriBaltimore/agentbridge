"""Bounded canary operations used by an actual Codex shell tool in offline tests."""

import json
from pathlib import Path
import sys
from urllib.request import urlopen


def main():
    result = {}
    for label, name in zip(('inside', 'outside'), sys.argv[1:3]):
        path = Path(name)
        try:
            result[label + '_read'] = path.read_text() == 'synthetic native canary'
        except OSError:
            result[label + '_read'] = False
        try:
            path.write_text('synthetic native write')
            result[label + '_write'] = True
        except OSError:
            result[label + '_write'] = False
    try:
        with urlopen(sys.argv[3], timeout=2) as response:
            result['loopback_network'] = response.status == 200
    except OSError:
        result['loopback_network'] = False
    external = Path('/proc') / sys.argv[4]
    result['outside_process_visible'] = external.exists()
    try:
        result['outside_process_environment_readable'] = bool((external / 'environ').read_bytes())
    except OSError:
        result['outside_process_environment_readable'] = False
    print('FIXTURE_NATIVE_ACCESS=' + json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
