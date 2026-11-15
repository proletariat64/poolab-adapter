"""Own carrier resources independently of the adapter's lifetime."""

import contextlib
import io
import json
import os
import pty
import queue
import selectors
import subprocess
import sys
import threading
import time
import tty

from .runtime import launch, terminate

MAX_CONFIG = 131072


def write_all(fd, data):
    while data:
        count = os.write(fd, data)
        if count <= 0:
            raise OSError("carrier write stalled")
        data = data[count:]


class ResourceOwner:
    def __init__(self, config, reader: io.BufferedReader):
        self.config = config
        self.reader = reader
        self.name = "poolab-adapter-" + config["nonce"]
        self.terminal = None
        self.attach = None
        self.master = None
        self.stop = threading.Event()
        self.creation_done = threading.Event()
        self.attached = threading.Event()
        self.state_lock = threading.Lock()
        self.cleanup_lock = threading.Lock()
        self.cleaned = False
        self.cleanup_ok = True
        self.pending = queue.Queue(maxsize=32)

    def command(self, *arguments):
        return launch("__exec__", str(os.getpid()), self.config["poolab"], *arguments)

    def rpc(self, arguments, timeout=5):
        process = subprocess.Popen(
            self.command(*arguments), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        assert process.stdout is not None
        selector = selectors.PollSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        output = bytearray()
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError("poolab operation timed out")
                for key, _ in selector.select(0.1):
                    data = os.read(key.fd, 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(data)
                    if len(output) > 65536:
                        raise ValueError("poolab response exceeded limit")
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
            if process.returncode:
                raise RuntimeError("poolab operation failed")
            return bytes(output)
        finally:
            selector.close()
            terminate(process)
            process.stdout.close()

    def cleanup(self):
        self.stop.set()
        self.attached.set()
        with self.cleanup_lock:
            if self.cleaned:
                return self.cleanup_ok
            try:
                with self.state_lock:
                    terminate(self.attach)
                    if self.master is not None:
                        os.close(self.master)
                        self.master = None
                terminals = [self.terminal] if self.terminal else []
                if not terminals:
                    # Creation may have succeeded remotely before its response was lost.
                    data = json.loads(self.rpc(["terminal", "ls", "--all", "--json", self.alias]))
                    items = data if isinstance(data, list) else data.get("terminals", [])
                    terminals = [item["id"] for item in items if item.get("name") == self.name]
                for terminal in terminals:
                    self.rpc(["terminal", "kill", terminal, self.alias])
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
                self.cleanup_ok = False
            self.cleaned = True
            return self.cleanup_ok

    @property
    def alias(self):
        return "--alias=" + self.config["alias"]

    def parent_input(self):
        try:
            while True:
                data = self.reader.read1(8192)
                if not data:
                    break
                self.pending.put_nowait(data)
        except (OSError, queue.Full):
            pass
        self.stop.set()
        self.creation_done.wait()
        ok = self.cleanup()
        # The stdout pump may be blocked because its parent disappeared.
        os._exit(0 if ok else 1)

    def terminal_input(self):
        self.attached.wait()
        try:
            if self.stop.is_set():
                return
            assert self.master is not None
            write_all(self.master, self.config["bootstrap"].encode("ascii") + b"\r")
            while not self.stop.is_set():
                try:
                    data = self.pending.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self.master is None:
                    return
                write_all(self.master, data)
        except (OSError, ValueError):
            self.stop.set()
            self.cleanup()
            os._exit(1)

    def run(self):
        threading.Thread(target=self.parent_input, daemon=True).start()
        try:
            created = json.loads(self.rpc(
                ["terminal", "create", "--json", "--cwd", "/", "--name", self.name, self.alias],
                timeout=min(5, self.config["startup_timeout"]),
            ))
            terminal = created.get("id")
            if not isinstance(terminal, str) or not terminal or any(c.isspace() for c in terminal):
                raise ValueError("invalid created terminal id")
            self.terminal = terminal
            self.creation_done.set()
            with self.state_lock:
                if self.stop.is_set():
                    return
                self.master, slave = pty.openpty()
                tty.setraw(slave)
                try:
                    self.attach = subprocess.Popen(
                        self.command("terminal", "attach", terminal, self.alias, "--detach-key="),
                        stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                finally:
                    os.close(slave)
            attach = self.attach
            if attach is None or attach.stdout is None:
                raise RuntimeError("attach was not initialized")
            header = {"owner": 1, "terminal": terminal, "name": self.name, "cli_pid": attach.pid}
            write_all(1, json.dumps(header).encode("ascii") + b"\n")
            self.attached.set()
            threading.Thread(target=self.terminal_input, daemon=True).start()
            while not self.stop.is_set():
                data = os.read(attach.stdout.fileno(), 65536)
                if not data:
                    break
                write_all(1, data)
        finally:
            self.creation_done.set()
            self.cleanup()
            if self.attach is not None and self.attach.stdout is not None:
                self.attach.stdout.close()


def main():
    owner = None
    try:
        reader = sys.stdin.buffer
        if not isinstance(reader, io.BufferedReader):
            raise ValueError("owner requires buffered pipe input")
        line = reader.readline(MAX_CONFIG + 1)
        if not line.endswith(b"\n") or len(line) > MAX_CONFIG:
            raise ValueError("invalid owner configuration")
        config = json.loads(line)
        owner = ResourceOwner(config, reader)
        owner.run()
        return 0 if owner.cleanup_ok else 1
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        with contextlib.suppress(OSError):
            write_all(2, b"herdr-poolab: carrier operation or cleanup failed\n")
        return 1
    finally:
        if owner is not None:
            owner.creation_done.set()
            owner.cleanup()
