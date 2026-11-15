"""Local executable dispatch and process lifetime helpers."""

import ctypes
import os
import signal
import subprocess
import sys
import zipfile
from pathlib import Path


def launch(mode, *arguments):
    executable = Path(sys.argv[0]).resolve()
    if executable.is_file() and zipfile.is_zipfile(executable):
        return [sys.executable, str(executable), mode, *arguments]
    return [sys.executable, "-m", "poolab_adapter", mode, *arguments]


def exec_child(arguments):
    try:
        parent = int(arguments[0])
        executable = arguments[1]
    except (IndexError, ValueError):
        return 64
    if parent <= 1:
        return 125
    if not os.path.isabs(executable):
        raise ValueError("backend executable must be absolute")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "parent-death signal failed")
    if os.getppid() != parent:
        return 125
    os.execv(executable, arguments[1:])  # noqa: S606 -- trusted absolute argv, never a local shell.
    return 125


def terminate(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except ProcessLookupError:
        process.wait()
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
