"""Cancellation for subprocesses launched in a dedicated POSIX session."""

from concurrent.futures import CancelledError
from contextlib import suppress
import os
import signal
import subprocess


def wait_cancellable(process, cancel, *, output_reader=None):
    """Wait for a process started with start_new_session=True and stop its group on abort."""
    while not cancel.is_set():
        try:
            return process.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            pass

    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    if output_reader is not None:
        # Children can keep the output pipe open after the process group leader exits.
        output_reader.join(timeout=1)
    if output_reader is None or not output_reader.is_alive():
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)
    if process.returncode is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    raise CancelledError("Operation aborted.")
