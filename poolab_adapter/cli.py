"""Command-line interface; Core supplies exactly one remote shell command."""

import argparse
import math
import os
import select
import shutil
import subprocess
import sys

from . import __version__


def diagnostic(message):
    data = ("herdr-poolab: " + message + "\n").encode("utf-8", errors="replace")[:512]
    poll = select.poll()
    poll.register(2, select.POLLOUT)
    try:
        if poll.poll(0):
            os.write(2, data)
    except OSError:
        pass


def duration(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a finite positive duration") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive duration")
    return number


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "__exec__":
        from .runtime import exec_child
        return exec_child(argv[1:])
    if argv and argv[0] == "__owner__":
        from .owner import main as owner_main
        return owner_main()
    parser = argparse.ArgumentParser(prog="herdr-poolab", description=__doc__)
    parser.add_argument("--version", action="version", version="herdr-poolab " + __version__)
    parser.add_argument("--alias", default="default")
    parser.add_argument("--startup-timeout", type=duration, default=10)
    parser.add_argument("--lease-timeout", type=duration, default=30)
    parser.add_argument("command", help="complete remote shell line, normally supplied by Herdr Core")
    options = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer is required")
    if not sys.platform.startswith("linux"):
        parser.error("this version requires Linux or WSL")
    if not options.alias or any(ord(character) < 32 or ord(character) == 127 for character in options.alias):
        parser.error("alias must be nonempty and contain no control characters")
    if not options.command.strip() or "\0" in options.command:
        parser.error("remote command must be nonempty and contain no NUL")
    backend = shutil.which("poolab")
    if backend is None:
        diagnostic("poolab was not found on PATH")
        return 255
    backend = os.path.abspath(backend)
    from .relay import DestinationClosed, Relay
    try:
        return Relay(options, backend).run()
    except DestinationClosed:
        return 141
    except (EOFError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        diagnostic(str(error))
        return 255
