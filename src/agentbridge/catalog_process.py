"""Bounded output and lifetime for a one-shot native catalog worker."""
import os
import select
import signal
import subprocess
import time

from .errors import BridgeError


MAX_OUTPUT_BYTES = 1024 * 1024


def read_output(command, *, env, timeout=30, max_bytes=MAX_OUTPUT_BYTES, cwd=None):
    """Read at most the output budget plus one overflow-detection byte.

    The deadline covers both stdout and worker exit. A private process group
    lets cleanup stop descendants that inherit stdout or outlive the worker.
    Stderr is discarded and provider bytes never enter exception messages.
    """
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env, cwd=cwd, start_new_session=True)
    except OSError:
        raise BridgeError('provider_unavailable', 'The model catalog worker could not be started.') from None
    output = bytearray()
    try:
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError('provider_timeout', 'The model catalog could not be read in time.')
            ready, _, _ = select.select([descriptor], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(descriptor, min(65536, max_bytes - len(output) + 1))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > max_bytes:
                raise BridgeError('provider_protocol_error', 'The provider model catalog exceeds the output limit.')
        process.wait(timeout=max(0, deadline - time.monotonic()))
        if process.returncode:
            raise BridgeError('provider_unavailable', 'The provider model catalog is unavailable.')
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise BridgeError('provider_timeout', 'The model catalog could not be read in time.') from None
    except OSError:
        raise BridgeError('provider_connection_lost', 'The model catalog worker connection failed.') from None
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            process.stdout.close()
            process.wait(timeout=5)
