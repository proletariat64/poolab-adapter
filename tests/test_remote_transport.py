"""Offline process/PTY tests. No poolab authentication or server is used."""
import base64
import contextlib
import json
import os
import pty
import select
import shlex
import signal
import subprocess
import sys
import time
import tty
import unittest
from pathlib import Path

WINDOW = 65536


class LocalCarrier:
    def __init__(self, command, **options):
        self.master, slave = pty.openpty()
        os.set_blocking(self.master, False)
        tty.setraw(slave)
        self.prefix = b"PA1:offline-test:"
        self.buffer = bytearray()
        module_dir = str(Path(__file__).resolve().parents[1] / "experiments")
        code = ("import sys,fcntl,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
                "sys.path.insert(0," + repr(module_dir) + ");"
                "from remote_probe import run;run('offline-test'," + repr(command)
                + ",isolate=False,**" + repr(options) + ")")
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-c", code], stdin=slave, stdout=slave,
                stderr=subprocess.PIPE, start_new_session=True,
            )
        finally:
            os.close(slave)

    def send(self, kind, data=b""):
        data = self.prefix + kind + b":" + data + b"\n"
        deadline = time.monotonic() + 3
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.master], [], remaining)[1]:
                raise TimeoutError("helper stopped consuming carrier input")
            data = data[os.write(self.master, data):]

    def frame(self, timeout: float = 3):
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer[:] = rest
                if not line:
                    continue
                if not line.startswith(self.prefix):
                    raise AssertionError("unframed helper output")
                kind, separator, data = line[len(self.prefix):].partition(b":")
                if not separator:
                    raise AssertionError("invalid frame")
                return bytes(kind), bytes(data)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.master], [], [], remaining)[0]:
                raise TimeoutError("no helper frame")
            try:
                chunk = os.read(self.master, 65536)
            except OSError as error:
                raise EOFError("helper exited") from error
            if not chunk:
                raise EOFError("helper exited")
            self.buffer.extend(chunk)

    def close(self):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=3)
        os.close(self.master)
        if self.process.stderr is not None:
            self.process.stderr.close()


class RemoteTransportTests(unittest.TestCase):
    def start(self, source, **options):
        carrier = LocalCarrier("exec python3 -u -c " + shlex.quote(source), **options)
        self.addCleanup(carrier.close)
        kind, data = carrier.frame()
        self.assertEqual(kind, b"R")
        return carrier, json.loads(data)

    def test_output_stops_at_receiver_window(self):
        carrier, _ = self.start("import os; os.write(1,b'x'*262144)")
        carrier.send(b"F")
        received = 0
        while received < WINDOW:
            kind, data = carrier.frame()
            self.assertEqual(kind, b"D")
            received += len(base64.b64decode(data, validate=True))
        self.assertEqual(received, WINDOW)
        with self.assertRaises(TimeoutError, msg="helper sent beyond receiver window"):
            carrier.frame(timeout=0.2)
        carrier.send(b"C", b"O:" + str(WINDOW).encode())
        while True:
            kind, data = carrier.frame()
            if kind == b"D":
                count = len(base64.b64decode(data, validate=True))
                received += count
                carrier.send(b"C", b"O:" + str(count).encode())
            elif kind == b"X":
                self.assertEqual(int(data), 0)
                carrier.send(b"A")
                break
            else:
                self.assertIn(kind, (b"O", b"S"))
        self.assertEqual(received, 262144)

    def test_lease_expires_without_local_liveness(self):
        carrier, ready = self.start("import time; time.sleep(60)", lease_seconds=0.4)
        carrier.process.wait(timeout=3)
        self.assertIsNotNone(carrier.process.returncode)
        self.assert_process_stopped(ready["pid"])

    def test_lease_expires_while_carrier_output_is_blocked(self):
        carrier, ready = self.start("import os; os.write(1,b'x'*16777216)",
                                    lease_seconds=0.4, flow_control=False)
        # Do not drain the PTY. The independent watchdog must still stop the group.
        carrier.process.wait(timeout=3)
        self.assert_process_stopped(ready["pid"])

    def test_heartbeat_works_while_child_stdin_is_blocked(self):
        carrier, ready = self.start("import time; time.sleep(60)", lease_seconds=0.4)
        for _ in range(WINDOW // 4096):
            carrier.send(b"D", base64.b64encode(b"x" * 4096))
        until = time.monotonic() + 1
        while time.monotonic() < until:
            carrier.send(b"H")
            time.sleep(0.05)
        self.assertIsNone(carrier.process.poll(), "blocked stdin prevented heartbeat handling")
        carrier.process.wait(timeout=3)
        self.assert_process_stopped(ready["pid"])

    def test_exit_record_is_held_until_acknowledged(self):
        carrier, _ = self.start("print('tail'); raise SystemExit(37)")
        carrier.send(b"F")
        output = bytearray()
        while True:
            kind, data = carrier.frame()
            if kind == b"D":
                decoded = base64.b64decode(data, validate=True)
                output.extend(decoded)
                carrier.send(b"C", b"O:" + str(len(decoded)).encode())
            elif kind == b"X":
                self.assertEqual(int(data), 37)
                break
        self.assertEqual(output, b"tail\n")
        self.assertIsNone(carrier.process.poll())
        carrier.send(b"A")
        self.assertEqual(carrier.process.wait(timeout=3), -signal.SIGKILL)

    def test_lease_stops_descendant_that_ignores_hangup(self):
        source = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',"
                  "'import signal,time;signal.signal(signal.SIGHUP,signal.SIG_IGN);time.sleep(60)']);"
                  "print(p.pid,flush=True);time.sleep(60)")
        carrier, ready = self.start(source, lease_seconds=0.5)
        kind, data = carrier.frame()
        self.assertEqual(kind, b"D")
        descendant = int(base64.b64decode(data))
        carrier.process.wait(timeout=3)
        self.assert_process_stopped(ready["pid"])
        self.assert_process_stopped(descendant)

    def test_ack_cleanup_stops_remaining_descendant(self):
        source = ("import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',"
                  "'import signal,time;signal.signal(signal.SIGHUP,signal.SIG_IGN);time.sleep(60)'],"
                  "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                  "print(p.pid,flush=True)")
        carrier, _ = self.start(source)
        carrier.send(b"F")
        descendant = None
        while True:
            kind, data = carrier.frame()
            if kind == b"D":
                decoded = base64.b64decode(data)
                descendant = int(decoded)
                carrier.send(b"C", b"O:" + str(len(decoded)).encode())
            elif kind == b"X":
                carrier.send(b"A")
                break
        carrier.process.wait(timeout=3)
        self.assertIsNotNone(descendant)
        self.assert_process_stopped(descendant)

    def test_cancel_stops_descendant_holding_output_pipes(self):
        source = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',"
                  "'import time;time.sleep(60)']);print(p.pid,flush=True);time.sleep(60)")
        carrier, _ = self.start(source)
        kind, data = carrier.frame()
        self.assertEqual(kind, b"D")
        decoded = base64.b64decode(data)
        descendant = int(decoded)
        carrier.send(b"C", b"O:" + str(len(decoded)).encode())
        carrier.send(b"K")
        while True:
            kind, data = carrier.frame(timeout=1)
            if kind == b"X":
                self.assertEqual(int(data), -signal.SIGTERM)
                carrier.send(b"A")
                break
        carrier.process.wait(timeout=3)
        self.assert_process_stopped(descendant)

    def test_cancel_deadline_stops_term_ignoring_descendant_despite_heartbeats(self):
        source = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',"
                  "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                  "print(\"child-ready\",flush=True);time.sleep(60)']);"
                  "print(p.pid,flush=True);time.sleep(60)")
        carrier, _ = self.start(source)
        output = bytearray()
        while b"child-ready\n" not in output or not any(line.isdigit() for line in output.splitlines()):
            kind, data = carrier.frame()
            self.assertEqual(kind, b"D")
            decoded = base64.b64decode(data)
            output.extend(decoded)
            carrier.send(b"C", b"O:" + str(len(decoded)).encode())
        descendant = next(int(line) for line in output.splitlines() if line.isdigit())
        carrier.send(b"K")
        deadline = time.monotonic() + 3
        while carrier.process.poll() is None and time.monotonic() < deadline:
            carrier.send(b"H")
            time.sleep(0.05)
        carrier.process.wait(timeout=0.5)
        self.assert_process_stopped(descendant)

    def test_small_input_frames_use_byte_credits_not_frame_count(self):
        carrier, _ = self.start("import time;time.sleep(60)")
        for _ in range(16384):
            carrier.send(b"D", base64.b64encode(b"x"))
        time.sleep(0.1)
        self.assertIsNone(carrier.process.poll(), "valid small frames exhausted a frame-count queue")

    def test_closed_child_stdin_is_reported_before_command_exit(self):
        carrier, _ = self.start("import os,time;os.close(0);print('stdout-open',flush=True);time.sleep(1)")
        kind, data = carrier.frame()
        self.assertEqual(kind, b"D")
        decoded = base64.b64decode(data)
        self.assertEqual(decoded, b"stdout-open\n")
        carrier.send(b"C", b"O:" + str(len(decoded)).encode())
        for _ in range(WINDOW // 4096):
            carrier.send(b"D", base64.b64encode(b"x" * 4096))
        while True:
            kind, data = carrier.frame()
            if kind == b"D":
                carrier.send(b"C", b"O:" + str(len(base64.b64decode(data))).encode())
            elif kind == b"N":
                self.assertEqual(data, b"")
                break
            elif kind == b"X":
                self.fail("command exited without reporting closed stdin")

    def assert_process_stopped(self, pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            stat = Path("/proc") / str(pid) / "stat"
            try:
                stopped = not stat.exists() or stat.read_text().split()[2] == "Z"
            except (FileNotFoundError, ProcessLookupError):
                stopped = True
            if stopped:
                return
            time.sleep(0.02)
        self.fail("owned remote-style child remained alive")


if __name__ == "__main__":
    unittest.main()
