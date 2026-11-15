"""Offline lifecycle regressions for the production PA2 remote helper."""

import array
import base64
import contextlib
import fcntl
import json
import os
import pty
import select
import shlex
import signal
import subprocess
import sys
import termios
import time
import tty
import unittest
from pathlib import Path


class ProductionCarrier:
    """Own one helper process group, a controlling PTY, and optional output pipe."""

    def __init__(self, source, *, lease=1, pipe_output=False):
        self.master, slave = pty.openpty()
        self.prefix = b"PA2:production-offline:"
        self.buffer = bytearray()
        self.owned_pids = []
        root = str(Path(__file__).resolve().parents[1])
        command = "exec " + shlex.quote(sys.executable) + " -u -c " + shlex.quote(source)
        code = (
            "import fcntl,termios,sys;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
            f"sys.path.insert(0,{root!r});"
            "from poolab_adapter.remote import run;"
            f"run('production-offline',{command!r},{lease!r})"
        )
        try:
            tty.setraw(slave)
            os.set_blocking(self.master, False)
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-c", code],
                stdin=slave,
                stdout=subprocess.PIPE if pipe_output else slave,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.output = self.master
        try:
            if self.process.stdout is not None:
                self.output = self.process.stdout.fileno()
                # A single 4096-byte data frame cannot fit in this carrier pipe.
                fcntl.fcntl(self.output, fcntl.F_SETPIPE_SZ, 4096)
                os.set_blocking(self.output, False)
        except BaseException:
            self.close()
            raise

    def send(self, kind, data=b""):
        wire = self.prefix + kind + b":" + data + b"\n"
        deadline = time.monotonic() + 3
        while wire:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.master], [], remaining)[1]:
                raise TimeoutError("production helper stopped reading the carrier")
            try:
                written = os.write(self.master, wire)
            except BlockingIOError:
                continue
            wire = wire[written:]

    def frame(self, timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer[:] = rest
                if not line:
                    continue
                if not line.startswith(self.prefix):
                    raise AssertionError("unframed production helper output")
                kind, separator, data = line[len(self.prefix):].partition(b":")
                if not separator:
                    raise AssertionError("invalid production helper frame")
                return bytes(kind), bytes(data)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.output], [], [], remaining)[0]:
                raise TimeoutError("no production helper frame")
            try:
                data = os.read(self.output, 65536)
            except OSError as error:
                raise EOFError("production helper exited") from error
            if not data:
                raise EOFError("production helper exited")
            self.buffer.extend(data)

    def close(self):
        # Only this invocation's freshly created process group is targeted.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        try:
            self.process.wait(timeout=5)
        finally:
            os.close(self.master)
            for pipe in (self.process.stdout, self.process.stderr):
                if pipe is not None:
                    pipe.close()


class ProductionRemoteTests(unittest.TestCase):
    def start(self, source, **options):
        carrier = ProductionCarrier(source, **options)
        self.addCleanup(carrier.close)
        kind, data = carrier.frame(timeout=5)
        self.assertEqual(kind, b"R")
        ready = json.loads(data)
        self.assertEqual((ready["version"], ready["slots"], ready["chunk"]), (2, 16, 4096))
        self.assertEqual(ready["helper_pid"], carrier.process.pid)
        self.assertEqual(ready["pgid"], carrier.process.pid)
        carrier.owned_pids.append(ready["pid"])
        return carrier

    def assert_stopped(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                state = (Path("/proc") / str(pid) / "stat").read_text().split(")", 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                return
            if state == "Z":
                return
            time.sleep(0.02)
        self.fail(f"owned child {pid} remained alive")

    def assert_group_stopped(self, carrier):
        self.assertEqual(carrier.process.wait(timeout=5), -signal.SIGKILL)
        for pid in carrier.owned_pids:
            self.assert_stopped(pid)

    def heartbeat(self, carrier):
        carrier.send(b"H")
        while True:
            kind, data = carrier.frame(timeout=1)
            if kind == b"P":
                self.assertEqual(data, b"")
                return
            self.assertIn(kind, (b"C", b"N"))

    def test_lease_expires_while_child_stdin_is_blocked(self):
        carrier = self.start(
            "import fcntl,time;fcntl.fcntl(0,fcntl.F_SETPIPE_SZ,4096);"
            "print('stdin-ready',flush=True);time.sleep(60)",
            lease=0.7,
        )
        kind, data = carrier.frame()
        self.assertEqual(kind, b"D")
        self.assertEqual(base64.b64decode(data, validate=True), b"stdin-ready\n")
        carrier.send(b"C", b"O:1")
        payload = base64.b64encode(b"x" * 4096)
        for _ in range(16):
            carrier.send(b"D", payload)
        # A non-reading child cannot consume all 16 slots. Heartbeats must
        # nevertheless work for longer than a lease, before liveness stops.
        until = time.monotonic() + 1.2
        while time.monotonic() < until:
            self.heartbeat(carrier)
            time.sleep(0.08)
        self.assertIsNone(carrier.process.poll())
        self.assert_group_stopped(carrier)

    def test_lease_expires_while_carrier_stdout_is_full(self):
        carrier = self.start(
            "import os,sys,time;sys.stdin.buffer.read();os.write(1,b'x'*4096);time.sleep(60)",
            lease=2,
            pipe_output=True,
        )
        self.assertFalse(carrier.buffer)
        capacity = fcntl.fcntl(carrier.output, fcntl.F_GETPIPE_SZ)
        carrier.send(b"F")
        deadline = time.monotonic() + 1.5
        while True:
            pending = array.array("i", [0])
            fcntl.ioctl(carrier.output, termios.FIONREAD, pending, True)
            if pending[0] == capacity:
                break
            if time.monotonic() >= deadline:
                self.fail("carrier stdout never filled")
            time.sleep(0.01)
        self.assertIsNone(carrier.process.poll())
        # No output credits were withheld: the FIRST data frame is larger than
        # the pipe, so frame() is blocked with 15 output slots still available.
        self.assert_group_stopped(carrier)

    def descendant(self, carrier):
        output = bytearray()
        deadline = time.monotonic() + 5
        while b"descendant-ready\n" not in output or not any(
            line.isdigit() for line in output.splitlines()
        ):
            kind, data = carrier.frame(timeout=max(0.01, deadline - time.monotonic()))
            self.assertEqual(kind, b"D")
            output.extend(base64.b64decode(data, validate=True))
            carrier.send(b"C", b"O:1")
        pid = next(int(line) for line in output.splitlines() if line.isdigit())
        carrier.owned_pids.append(pid)
        return pid

    def test_cancel_deadline_kills_term_ignoring_descendant_despite_heartbeats(self):
        descendant = (
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "print('descendant-ready',flush=True);time.sleep(60)"
        )
        source = (
            "import subprocess,sys,time;"
            f"p=subprocess.Popen([sys.executable,'-u','-c',{descendant!r}]);"
            "print(p.pid,flush=True);time.sleep(60)"
        )
        for cancel in ("frame", "signal"):
            with self.subTest(cancel=cancel):
                carrier = self.start(source, lease=1)
                self.descendant(carrier)
                self.heartbeat(carrier)
                started = time.monotonic()
                if cancel == "frame":
                    carrier.send(b"K")
                else:
                    os.kill(carrier.process.pid, signal.SIGTERM)
                # The descendant retains stdin/stdout/stderr. No EOF or final ACK
                # can help cleanup; pongs keep the ordinary lease alive.
                pongs = 0
                while carrier.process.poll() is None and time.monotonic() - started < 5:
                    try:
                        self.heartbeat(carrier)
                    except (EOFError, OSError):
                        break
                    pongs += 1
                    time.sleep(0.08)
                self.assert_group_stopped(carrier)
                if cancel == "frame":
                    self.assertGreater(pongs, 2)
                    self.assertGreaterEqual(time.monotonic() - started, 1.7)
                # Direct helper signals intentionally kill immediately; only K
                # requests the two-second graceful cancellation window.
                self.assertLess(time.monotonic() - started, 5)

    def test_final_ack_kills_remaining_descendant(self):
        descendant = (
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "time.sleep(60)"
        )
        source = (
            "import subprocess,sys;"
            f"p=subprocess.Popen([sys.executable,'-u','-c',{descendant!r}],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            "print(p.pid,flush=True);print('descendant-ready',flush=True)"
        )
        carrier = self.start(source, lease=5)
        self.descendant(carrier)
        carrier.send(b"F")
        while True:
            kind, data = carrier.frame()
            if kind == b"X":
                result = json.loads(data)
                self.assertEqual(result["status"], 0)
                self.assertEqual(result["stdin"], 0)
                self.assertGreater(result["stdout"], 0)
                self.assertEqual(result["stderr"], 0)
                break
            self.assertIn(kind, (b"N", b"O", b"S"))
        self.assertIsNone(carrier.process.poll())
        state = (Path("/proc") / str(carrier.owned_pids[-1]) / "stat").read_text()
        self.assertNotEqual(state.split(")", 1)[1].split()[0], "Z")
        carrier.send(b"A")
        self.assert_group_stopped(carrier)


if __name__ == "__main__":
    unittest.main()
