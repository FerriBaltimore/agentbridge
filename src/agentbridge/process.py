"""Observe process identity without signalling unrelated reused PIDs."""
import os
from pathlib import Path
import sys


def identity(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return None
    if sys.platform.startswith('linux'):
        try:
            stat=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
            if stat[0]=='Z':return None
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return f'{boot}:{stat[19]}'
        except (OSError,IndexError):return None
    # Unix fallback uses process start timestamp. Linux is the tested delivery target.
    import subprocess
    try:
        result=subprocess.run(['ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,timeout=2)
        return result.stdout.strip() or None
    except (OSError,subprocess.TimeoutExpired):return None


def alive(pid, recorded):
    return bool(recorded and identity(pid)==recorded)
