"""Bounded opaque-stdio relay at the adapter executable boundary."""

import base64
import contextlib
import json
import os
import selectors
import shlex
import signal
import subprocess
import time
import uuid
import zlib
from collections import deque
from importlib.resources import files

from .remote import CHUNK, MAX_FRAME, SLOTS
from .runtime import launch


class DestinationClosed(BrokenPipeError):
    """A local stdout/stderr consumer closed its pipe."""


class Relay:
    def __init__(self, options, backend):
        self.options = options
        self.nonce = uuid.uuid4().hex
        self.prefix = ("PA2:" + self.nonce + ":").encode("ascii")
        self.backend = backend
        self.process = None
        self.raw = bytearray()
        self.outgoing = bytearray()
        self.header = False
        self.ready = False
        self.bootstrap_sent = False
        self.bootstrap_payload = b""
        self.owner_output = True
        self.owner_error = True
        self.input_slots = SLOTS
        self.input_stopped = False
        self.input_open = True
        self.input_eof = False
        self.input_bytes = 0
        self.queues = {b"O": deque(), b"E": deque()}
        self.allowance = {b"O": SLOTS, b"E": SLOTS}
        self.received = {b"O": 0, b"E": 0}
        self.delivered = {b"O": 0, b"E": 0}
        self.eof = set()
        self.result = None
        self.finalising = False
        self.interrupted = 0
        self.cancel_deadline = None
        self.diagnostics = 0
        self.sinks = {}
        self.original_blocking = {}
        self.owned_fds = []
        self.original_signals = {}
        self.started = time.monotonic()
        self.last_receive = self.started
        self.next_heartbeat = self.started

    def queue(self, kind, data=b""):
        frame = self.prefix + kind + b":" + data + b"\n"
        if len(frame) > MAX_FRAME or len(self.outgoing) + len(frame) > 262144:
            raise ValueError("carrier frame or pending-wire limit exceeded")
        self.outgoing.extend(frame)

    def configure(self):
        for stream, fd in ((b"O", 1), (b"E", 2)):
            if os.isatty(fd):
                target = os.open(os.ttyname(fd), os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
                self.owned_fds.append(target)
            else:
                target = fd
                self.original_blocking[fd] = os.get_blocking(fd)
                os.set_blocking(fd, False)
            self.sinks[stream] = target
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self.original_signals[number] = signal.getsignal(number)
            signal.signal(number, self.on_signal)

    def on_signal(self, number, _frame):
        self.interrupted = number

    def close_input(self):
        if self.input_open:
            os.close(0)
            self.input_open = False

    def start_owner(self):
        source = files("poolab_adapter").joinpath("remote.py").read_text(encoding="utf-8")
        source += "\nrun(" + repr(self.nonce) + "," + repr(self.options.command) + "," + repr(self.options.lease_timeout) + ")\n"
        self.bootstrap_payload = base64.b64encode(zlib.compress(source.encode("utf-8")))
        if len(self.bootstrap_payload) > 65536:
            raise ValueError("remote bootstrap payload exceeds limit")
        load = ("b=bytearray()\nwhile len(b)<n:\n x=os.read(0,min(4096,n-len(b)))\n"
                " if not x: raise EOFError('bootstrap closed')\n b.extend(x)\n")
        marker = b"\n" + self.prefix + b"B:\n"
        code = ("import os,tty,base64,zlib;tty.setraw(0);os.write(1," + repr(marker) + ");"
                "n=" + str(len(self.bootstrap_payload)) + ";exec(" + repr(load) + ");"
                "exec(zlib.decompress(base64.b64decode(b,validate=True)))")
        bootstrap = "exec python3 -u -c " + shlex.quote(code)
        config = json.dumps({"alias": self.options.alias, "nonce": self.nonce,
                             "poolab": self.backend, "bootstrap": bootstrap,
                             "startup_timeout": self.options.startup_timeout}).encode("ascii") + b"\n"
        if len(config) > 131072:
            raise ValueError("remote bootstrap exceeds configuration limit")
        self.process = subprocess.Popen(
            launch("__owner__"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        assert self.process.stderr is not None
        self.process.stdin.write(config)
        self.process.stdin.flush()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            os.set_blocking(stream.fileno(), False)

    def handle(self, kind, data):
        if not self.ready:
            if kind == b"B" and not data and not self.bootstrap_sent:
                self.outgoing.extend(self.bootstrap_payload)
                self.bootstrap_sent = True
                return
            if kind != b"R" or not self.bootstrap_sent:
                raise ValueError("carrier did not begin with READY")
            metadata = json.loads(data)
            if (metadata.get("version") != 2 or metadata.get("slots") != SLOTS
                    or metadata.get("chunk") != CHUNK):
                raise ValueError("unsupported carrier contract")
            self.ready = True
            self.next_heartbeat = time.monotonic()
            return
        if kind in (b"D", b"E"):
            stream = b"O" if kind == b"D" else b"E"
            decoded = base64.b64decode(data, validate=True)
            if (stream in self.eof or self.result is not None or self.allowance[stream] <= 0
                    or not 0 < len(decoded) <= CHUNK):
                raise ValueError("invalid output stream state or allowance")
            self.allowance[stream] -= 1
            self.queues[stream].append(decoded)
            self.received[stream] += len(decoded)
        elif kind == b"C":
            stream, separator, count = data.partition(b":")
            if stream != b"I" or not separator or not count.isdigit() or len(count) > 2:
                raise ValueError("invalid input credit")
            amount = int(count)
            if amount <= 0 or self.input_slots + amount > SLOTS:
                raise ValueError("input allowance exceeded")
            self.input_slots += amount
        elif kind == b"N" and not data:
            self.input_stopped = True
            self.close_input()
        elif kind in (b"O", b"S") and not data:
            stream = b"O" if kind == b"O" else b"E"
            if stream in self.eof or self.result is not None:
                raise ValueError("duplicate or late stream EOF")
            self.eof.add(stream)
        elif kind == b"X":
            if self.result is not None or self.eof != {b"O", b"E"}:
                raise ValueError("exit before stream completion")
            result = json.loads(data)
            for name in ("status", "stdin", "stdout", "stderr"):
                if type(result.get(name)) is not int:
                    raise ValueError("invalid final status counters")
            if (not -127 <= result["status"] <= 255 or not 0 <= result["stdin"] <= self.input_bytes
                    or result["stdout"] != self.received[b"O"]
                    or result["stderr"] != self.received[b"E"]):
                raise ValueError("final stream count mismatch")
            self.result = result
            self.input_stopped = True
            self.close_input()
        elif kind == b"P" and not data:
            pass
        else:
            raise ValueError("invalid carrier message")

    def feed(self, data):
        self.raw.extend(data)
        if len(self.raw) > 131072:
            raise ValueError("carrier parse buffer exceeded")
        while b"\n" in self.raw:
            line, _, rest = self.raw.partition(b"\n")
            self.raw[:] = rest
            if not self.header:
                header = json.loads(line)
                if header.get("owner") != 1:
                    raise ValueError("invalid resource owner header")
                self.header = True
                continue
            if not line.startswith(self.prefix):
                if self.ready:
                    raise ValueError("unframed carrier output")
                self.diagnostics += len(line) + 1
                if self.diagnostics > 65536:
                    raise ValueError("startup output exceeded limit")
                continue
            if len(line) + 1 > MAX_FRAME:
                raise ValueError("carrier frame exceeded limit")
            kind, separator, payload = line[len(self.prefix):].partition(b":")
            if not separator:
                raise ValueError("invalid carrier frame")
            self.handle(bytes(kind), bytes(payload))
            self.last_receive = time.monotonic()
        if self.ready and len(self.raw) > MAX_FRAME:
            raise ValueError("unterminated carrier frame")

    def deliver(self, stream):
        data = self.queues[stream][0]
        try:
            count = os.write(self.sinks[stream], data)
        except BlockingIOError:
            return
        except BrokenPipeError:
            raise DestinationClosed("local destination closed") from None
        if count <= 0:
            raise OSError("destination write made no progress")
        self.delivered[stream] += count
        if count == len(data):
            self.queues[stream].popleft()
            self.allowance[stream] += 1
            self.queue(b"C", stream + b":1")
        else:
            self.queues[stream][0] = data[count:]

    def tick(self):
        now = time.monotonic()
        if self.interrupted and self.cancel_deadline is None:
            self.input_stopped = True
            self.close_input()
            if self.ready:
                self.queue(b"K")
            self.cancel_deadline = now + 3
        if self.cancel_deadline is not None and now >= self.cancel_deadline:
            return True
        if not self.ready and now - self.started > self.options.startup_timeout:
            raise TimeoutError("carrier startup timed out; check the poolab alias and authentication")
        if self.ready and now - self.last_receive > self.options.lease_timeout:
            raise TimeoutError("remote carrier liveness expired")
        if self.ready and not self.finalising and now >= self.next_heartbeat:
            self.queue(b"H")
            self.next_heartbeat = now + min(5, self.options.lease_timeout / 5)
        if self.result is not None and not any(self.queues.values()) and not self.finalising:
            if self.delivered != self.received:
                raise ValueError("destination delivery count mismatch")
            self.queue(b"A")
            self.finalising = True
        return self.finalising and not self.outgoing

    def loop(self):
        assert self.process is not None and self.process.stdin is not None
        assert self.process.stdout is not None and self.process.stderr is not None
        while not self.tick():
            with selectors.PollSelector() as selector:
                if self.owner_output:
                    selector.register(self.process.stdout, selectors.EVENT_READ, "carrier")
                if self.owner_error:
                    selector.register(self.process.stderr, selectors.EVENT_READ, "diagnostics")
                if self.outgoing:
                    selector.register(self.process.stdin, selectors.EVENT_WRITE, "wire")
                if self.ready and not self.input_eof and not self.input_stopped and self.input_slots:
                    selector.register(0, selectors.EVENT_READ, "input")
                for stream in (b"O", b"E"):
                    if self.queues[stream]:
                        selector.register(self.sinks[stream], selectors.EVENT_WRITE, stream)
                interval = 0.1
                if self.ready and not self.finalising:
                    interval = min(interval, max(0.001, self.next_heartbeat - time.monotonic()))
                events = selector.select(interval)
            for key, _ in events:
                if key.data == "carrier":
                    data = os.read(key.fd, 65536)
                    if data:
                        self.feed(data)
                    else:
                        self.owner_output = False
                        if self.interrupted:
                            return 128 + self.interrupted
                        if not self.finalising:
                            raise EOFError("carrier closed before final delivery")
                elif key.data == "diagnostics":
                    data = os.read(key.fd, 4096)
                    if not data:
                        self.owner_error = False
                elif key.data == "wire":
                    try:
                        count = os.write(key.fd, self.outgoing)
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        raise EOFError("resource owner closed carrier input") from None
                    if count <= 0:
                        raise OSError("carrier input write stalled")
                    del self.outgoing[:count]
                elif key.data == "input":
                    if self.input_stopped or not self.input_slots:
                        continue
                    data = os.read(0, CHUNK)
                    if data:
                        self.queue(b"D", base64.b64encode(data))
                        self.input_slots -= 1
                        self.input_bytes += len(data)
                    else:
                        self.input_eof = True
                        self.close_input()
                        self.queue(b"F")
                else:
                    self.deliver(key.data)
        if self.interrupted:
            return 128 + self.interrupted
        assert self.result is not None
        status = self.result["status"]
        return status if status >= 0 else 128 - status

    def run(self):
        code = None
        try:
            self.configure()
            self.start_owner()
            code = self.loop()
        finally:
            with contextlib.suppress(OSError):
                self.close_input()
            try:
                if self.process is not None:
                    if self.process.stdin is not None:
                        with contextlib.suppress(OSError):
                            self.process.stdin.close()
                    try:
                        self.process.wait(timeout=35)
                    finally:
                        for stream in (self.process.stdout, self.process.stderr):
                            if stream is not None:
                                stream.close()
            finally:
                for fd in self.owned_fds:
                    os.close(fd)
                for fd, blocking in self.original_blocking.items():
                    os.set_blocking(fd, blocking)
                for number, handler in self.original_signals.items():
                    signal.signal(number, handler)
        if self.process is not None and self.process.returncode:
            raise RuntimeError("carrier resource cleanup was not confirmed")
        assert code is not None
        return code
