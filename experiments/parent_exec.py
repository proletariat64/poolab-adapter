"""Linux child launcher: do not leave an attach client after its owner dies."""
import ctypes
import os
import signal
import sys


def main():
    try:
        parent = int(sys.argv[1])
    except (IndexError, ValueError):
        raise SystemExit("expected parent PID and absolute child executable") from None
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "cannot set parent-death signal")
    if os.getppid() != parent:
        raise SystemExit(125)
    if not os.path.isabs(sys.argv[2]):
        raise ValueError("child executable must be an absolute path")
    # Replace the guard, preserving the parent-death signal. Never invoke a shell.
    os.execv(sys.argv[2], sys.argv[2:])  # noqa: S606, RUF100 -- trusted absolute executable and argv.


if __name__ == "__main__":
    main()
