"""Execute supervision in its own worker process; never change pytest's subreaper state."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from agentbridge.checkpoint.processes import enable_subreaper, quiesce
from agentbridge.process import alive, identity


def main():
    enable_subreaper()
    marker = Path(sys.argv[1]) / 'escaped-pid'
    launcher = Path(__file__).with_name('test_checkpoint_escaped_child.py')
    child = subprocess.Popen([sys.executable, str(launcher), str(marker)])
    child.wait(timeout=5)
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    pid = int(marker.read_text())
    recorded = identity(pid)
    assert alive(pid, recorded)
    result = quiesce()
    os.waitpid(pid, 0)
    print(json.dumps({'quiesced': result, 'escaped_stopped': not alive(pid, recorded)}))


if __name__ == '__main__':
    main()
