"""Orphan a new-session child to exercise the worker's subreaper fence."""

import os
from pathlib import Path
import sys
import time


def main():
    child = os.fork()
    if child:
        return
    os.setsid()
    Path(sys.argv[1]).write_text(str(os.getpid()))
    time.sleep(30)


if __name__ == '__main__':
    main()
