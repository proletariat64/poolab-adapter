#!/usr/bin/env python3
"""Local poolab CLI test double: real PTYs/processes, no network or authentication."""

import contextlib
import json
import os
import pty
import selectors
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ["FAKE_POOLAB_STATE"])
ROOT.mkdir(parents=True, exist_ok=True)


def event(action, **fields):
    with (ROOT / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"action": action, **fields}) + "\n")


def store(item):
    temporary = ROOT / (item["id"] + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(item))
    temporary.replace(ROOT / (item["id"] + ".json"))


def value(args, name, default=None):
    for index, arg in enumerate(args):
        if arg == name:
            return args[index + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return default


def write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def main():
    args = sys.argv[1:]
    if args[:2] == ["terminal", "create"]:
        item = {"id": "term-" + uuid.uuid4().hex, "name": value(args, "--name"), "pid": None}
        store(item)
        event("create", **item)
        time.sleep(float(os.environ.get("FAKE_CREATE_DELAY", "0")))
        print(json.dumps(item), flush=True)
        return 0
    if args[:2] == ["terminal", "ls"]:
        print(json.dumps([json.loads(path.read_text()) for path in ROOT.glob("term-*.json")]))
        return 0
    if args[:2] == ["terminal", "kill"]:
        terminal = args[2]
        path = ROOT / (terminal + ".json")
        if path.exists():
            item = json.loads(path.read_text())
            if item["pid"]:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(item["pid"], signal.SIGKILL)
            path.unlink(missing_ok=True)
        event("kill", id=terminal)
        return 0
    if args[:2] != ["terminal", "attach"]:
        raise SystemExit("unsupported fake poolab command")
    if not os.isatty(0):
        raise SystemExit("fake attach requires a private TTY for input")
    terminal = args[2]
    item = json.loads((ROOT / (terminal + ".json")).read_text())
    master, slave = pty.openpty()
    launcher = ("import os,fcntl,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
                "os.execv('/bin/sh',['sh','-i'])")
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", launcher], stdin=slave, stdout=slave,
        stderr=slave, start_new_session=True,
    )
    os.close(slave)
    item["pid"] = process.pid
    store(item)
    event("attach", **item)
    print("fake-backend-private-diagnostic", file=sys.stderr, flush=True)
    fault = os.environ.get("FAKE_CARRIER_FAULT")
    output = bytearray()
    selector = selectors.PollSelector()
    selector.register(0, selectors.EVENT_READ, "input")
    selector.register(master, selectors.EVENT_READ, "output")
    try:
        while selector.get_map():
            for key, _ in selector.select(0.1):
                try:
                    data = os.read(key.fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    if key.data == "output":
                        return 0
                    selector.unregister(key.fd)
                elif key.data == "input":
                    write_all(master, data)
                elif fault:
                    output.extend(data)
                    while b"\n" in output:
                        line, _, rest = output.partition(b"\n")
                        output[:] = rest
                        fields = line.split(b":", 3)
                        if len(fields) == 4 and fields[0] == b"PA2" and fields[2] == b"X":
                            if fault == "truncate-final":
                                return 0
                            result = json.loads(fields[3])
                            result["stdout"] += 1
                            line = b":".join(fields[:3]) + b":" + json.dumps(result).encode()
                        write_all(1, line + b"\n")
                else:
                    write_all(1, data)
    finally:
        # Attach intentionally does not kill its terminal. The resource owner must.
        selector.close()
        os.close(master)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
