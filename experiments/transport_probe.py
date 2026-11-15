"""Opt-in configured-target transport experiments. Creates and kills only its own terminals.

Run from the repository root: python3 experiments/transport_probe.py
This is a design experiment, not an hc --via executable.
"""
import base64
import contextlib
import json
import os
import pty
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tty
import uuid
import zlib
from pathlib import Path


class Channel:
    def __init__(self, command, *, direct: bool | str = False, kill_on_detach=False,
                 isolate=False, flow_control=True, lease_seconds=30, heartbeat_interval=5):
        self.terminal = None
        self.process = None
        self.master = None
        self.selector = selectors.DefaultSelector()
        self.buffer = bytearray()
        self.startup = bytearray()
        self.nonce = uuid.uuid4().hex
        self.prefix = ("PA1:" + self.nonce + ":").encode()
        self.write_lock = threading.Lock()
        self.credit_condition = threading.Condition()
        self.input_credit = 65536
        self.flow_control = flow_control
        self.closed = threading.Event()
        self.input_stopped = threading.Event()
        self.stdin_closed_by_peer = False
        self.heartbeat = None
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_error = None
        poolab = shutil.which("poolab")
        if poolab is None:
            raise FileNotFoundError("poolab is not installed")
        try:
            created = subprocess.run(
                [poolab, "terminal", "create", "--alias", "default",
                 "--name", "adapter-probe-" + self.nonce[:12], "--json"],
                capture_output=True, check=True, timeout=20,
            )
            self.terminal = json.loads(created.stdout)["id"]
            self.master, slave = pty.openpty()
            tty.setraw(slave)
            attach = [poolab, "terminal", "attach", self.terminal, "--alias", "default",
                      "--detach-key="] + (["--kill-on-detach"] if kill_on_detach else [])
            guard = str(Path(__file__).with_name("parent_exec.py"))
            try:
                self.process = subprocess.Popen(
                    [sys.executable, guard, str(os.getpid()), *attach], stdin=slave,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                )
            finally:
                os.close(slave)
            assert self.process.stdout is not None and self.process.stderr is not None
            self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
            self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
            source = Path(__file__).with_name("remote_probe.py").read_text()
            source += ("\nrun(" + repr(self.nonce) + "," + repr(command) + "," + repr(direct)
                       + "," + repr(isolate) + "," + repr(flow_control) + "," + repr(lease_seconds) + ")\n")
            packed = base64.b64encode(zlib.compress(source.encode())).decode()
            bootstrap = "import base64,zlib;exec(zlib.decompress(base64.b64decode(" + repr(packed) + ")))"
            self.bootstrap_bytes = len(bootstrap.encode())
            time.sleep(0.5)
            self.write(("exec python3 -u -c " + shlex.quote(bootstrap) + "\r").encode())
        except BaseException:
            self.close()
            raise

    def write(self, data):
        with self.write_lock:
            if self.closed.is_set():
                raise OSError("channel is closed")
            assert self.master is not None
            while data:
                data = data[os.write(self.master, data):]

    def send(self, kind, data=b""):
        if kind == b"D" and self.flow_control:
            size = len(base64.b64decode(data, validate=True))
            if not 0 < size <= 4096:
                raise ValueError("invalid stdin frame size")
            with self.credit_condition:
                self.credit_condition.wait_for(
                    lambda: self.input_credit >= size or self.closed.is_set() or self.input_stopped.is_set())
                if self.closed.is_set():
                    raise OSError("channel is closed")
                if self.input_stopped.is_set():
                    raise BrokenPipeError("remote command stopped accepting stdin")
                self.input_credit -= size
        wire = self.prefix + kind + b":" + data + b"\n"
        if len(wire) > 8192:
            raise ValueError("frame exceeds wire limit")
        self.write(wire)

    def start_heartbeat(self):
        if self.heartbeat is not None or self.heartbeat_interval <= 0:
            return

        def beat():
            while not self.closed.wait(self.heartbeat_interval):
                try:
                    self.send(b"H")
                except OSError as error:
                    if not self.closed.is_set():
                        self.heartbeat_error = error
                    return

        self.heartbeat = threading.Thread(target=beat, daemon=True)
        self.heartbeat.start()

    def read_more(self, deadline):
        if self.heartbeat_error is not None:
            raise OSError("carrier heartbeat failed") from self.heartbeat_error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("transport probe deadline exceeded")
        for key, _ in self.selector.select(min(remaining, 1)):
            data = os.read(key.fd, 65536)
            if not data:
                self.selector.unregister(key.fileobj)
                if key.data == "stdout":
                    raise EOFError("poolab output closed; buffered=" + str(len(self.buffer))
                                   + "; tail=" + repr(bytes(self.buffer[-120:])))
            elif key.data == "stdout":
                self.buffer.extend(data)
                if len(self.buffer) > 262144:
                    raise ValueError("carrier output buffer exceeded")
            else:
                self.startup.extend(data)
                if len(self.startup) > 65536:
                    raise ValueError("startup diagnostics exceeded")

    def frame(self, deadline, *, startup=False):
        while True:
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer[:] = rest
                if line.startswith(self.prefix):
                    if len(line) + 1 > 8192:
                        raise ValueError("carrier frame exceeded")
                    kind, separator, data = line[len(self.prefix):].partition(b":")
                    if not separator:
                        raise ValueError("missing frame separator")
                    if kind == b"C":
                        stream, _, count = data.partition(b":")
                        try:
                            amount = int(count)
                        except ValueError:
                            raise ValueError("invalid stdin credit count") from None
                        with self.credit_condition:
                            if (stream != b"I" or amount <= 0
                                    or self.input_credit + amount > 65536):
                                raise ValueError("invalid stdin credit")
                            self.input_credit += amount
                            self.credit_condition.notify_all()
                        continue
                    if kind in (b"N", b"X"):
                        with self.credit_condition:
                            self.input_stopped.set()
                            self.credit_condition.notify_all()
                        if kind == b"N":
                            self.stdin_closed_by_peer = True
                            continue
                    if kind == b"R":
                        self.start_heartbeat()
                    return bytes(kind), bytes(data)
                if not startup:
                    raise ValueError("unframed output after READY")
                self.startup.extend(line + b"\n")
                if len(self.startup) > 65536:
                    raise ValueError("startup output exceeded")
            if not startup and len(self.buffer) > 8192:
                raise ValueError("unterminated carrier frame exceeded")
            self.read_more(deadline)

    def close(self):
        self.closed.set()
        with self.credit_condition:
            self.credit_condition.notify_all()
        try:
            if self.process is not None and self.process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=3)
            if self.master is not None:
                os.close(self.master)
                self.master = None
            self.selector.close()
            if self.process is not None:
                for stream in (self.process.stdout, self.process.stderr):
                    if stream is not None:
                        stream.close()
            if self.heartbeat is not None:
                self.heartbeat.join(timeout=2)
        finally:
            if self.terminal is not None:
                subprocess.run(
                    ["poolab", "terminal", "kill", "--alias", "default", self.terminal],
                    check=True, capture_output=True, timeout=20,
                )
                self.terminal = None


def python_command(source):
    return "exec python3 -u -c " + shlex.quote(source)


def framed_test(name, source, payload, expected_stdout, expected_stderr=b"", expected_rc=0,
                *, pause=0, cancel=False, flow_control=True, allow_stdin_close=False):
    started = time.monotonic()
    channel = Channel(python_command(source), flow_control=flow_control)
    sender = None
    errors = []
    input_state = {"sent": 0, "stopped": False}
    try:
        deadline = time.monotonic() + 90
        kind, metadata = channel.frame(deadline, startup=True)
        assert kind == b"R", (kind, metadata)
        ready = json.loads(metadata)

        def send_input():
            try:
                for offset in range(0, len(payload), 4096):
                    data = payload[offset:offset + 4096]
                    channel.send(b"D", base64.b64encode(data))
                    input_state["sent"] += len(data)
                channel.send(b"K" if cancel else b"F")
            except BrokenPipeError:
                input_state["stopped"] = True
            except (OSError, ValueError) as error:
                errors.append(str(error))

        sender = threading.Thread(target=send_input, daemon=True)
        sender.start()
        time.sleep(pause)
        output, stderr = bytearray(), bytearray()
        eof = set()
        while True:
            kind, data = channel.frame(deadline)
            if kind in (b"D", b"E"):
                decoded = base64.b64decode(data, validate=True)
                (output if kind == b"D" else stderr).extend(decoded)
                if flow_control:
                    stream = b"O" if kind == b"D" else b"E"
                    channel.send(b"C", stream + b":" + str(len(decoded)).encode())
            elif kind in (b"O", b"S"):
                eof.add(kind)
            elif kind == b"X":
                rc = int(data)
                channel.send(b"A")
                break
            else:
                raise ValueError((kind, data))
        sender.join(timeout=2)
        assert not sender.is_alive(), "input did not finish"
        assert not errors, errors
        assert not input_state["stopped"] or allow_stdin_close or cancel, "remote stdin closed before all input was sent"
        if expected_stdout is not None:
            assert bytes(output) == expected_stdout, ("stdout mismatch", len(output), len(expected_stdout))
        assert bytes(stderr) == expected_stderr, ("stderr mismatch", len(stderr), len(expected_stderr))
        assert rc == expected_rc, (rc, expected_rc)
        assert eof == {b"O", b"S"}, eof
        return {"name": name, "passed": True, "seconds": round(time.monotonic() - started, 3),
                "input_bytes": len(payload), "input_bytes_sent": input_state["sent"],
                "input_stopped_early": input_state["stopped"],
                "stdin_closed_by_peer": channel.stdin_closed_by_peer,
                "stdout_bytes": len(output), "stderr_bytes": len(stderr),
                "exit": rc, "ready": ready, "bootstrap_bytes": channel.bootstrap_bytes,
                "startup_bytes_discarded": len(channel.startup), "consumer_pause_seconds": pause,
                "flow_control": flow_control,
                "stdout_preview": bytes(output).decode("ascii", "backslashreplace") if len(output) < 4096 else None}
    finally:
        channel.close()
        if sender is not None:
            sender.join(timeout=2)


def direct_test(*, echo=False):
    channel = Channel("", direct="echo" if echo else True)
    try:
        deadline = time.monotonic() + 15
        begin, end = channel.prefix + b"BEGIN\n", channel.prefix + b"END\n"
        while begin not in channel.buffer:
            channel.read_more(deadline)
        if echo:
            channel.write(bytes(range(256)) * 16)
        while end not in channel.buffer:
            channel.read_more(deadline)
        raw = bytes(channel.buffer).split(begin, 1)[1].split(end, 1)[0]
        expected = bytes(range(256)) * 16
        channel.write(b"\n")
        return {"name": "raw_pty_echo" if echo else "raw_pty_output", "equal": raw == expected,
                "expected_bytes": len(expected), "received_bytes": len(raw),
                "utf8_replacement_sequences": raw.count(b"\xef\xbf\xbd")}
    finally:
        channel.close()


def disconnect_test(signum, *, isolate):
    channel = Channel(python_command("import time; time.sleep(120)"),
                      kill_on_detach=True, isolate=isolate)
    pid = None
    cleanup_source = None
    try:
        kind, data = channel.frame(time.monotonic() + 15, startup=True)
        assert kind == b"R"
        pid = json.loads(data)["pid"]
        source = ("from pathlib import Path; p=Path('/proc/" + str(pid) + "/stat'); "
                  "print(p.read_text().split()[2] if p.exists() else '-')")
        cleanup_source = ("from pathlib import Path; import os,signal; p=Path('/proc/" + str(pid) + "'); "
                          "expected=[b'python3',b'-u',b'-c',b'import time; time.sleep(120)',b'']; "
                          "exec(\"if p.exists() and p.joinpath('cmdline').read_bytes().split(b'\\\\0')==expected:\\n "
                          "os.kill(" + str(pid) + ",signal.SIGKILL)\")")
        assert channel.process is not None
        os.killpg(channel.process.pid, signum)
        channel.process.wait(timeout=5)
        time.sleep(0.3)
        observed = framed_test("observe_disconnect_child", source, b"", None)
        disconnect_state = observed["stdout_preview"].strip()
        channel.close()
        observed = framed_test("observe_terminal_kill_child", source, b"", None)
        terminal_kill_state = observed["stdout_preview"].strip()
    finally:
        try:
            channel.close()
        finally:
            if cleanup_source is not None:
                framed_test("cleanup_exact_probe_child", cleanup_source, b"", b"")
    after_cleanup = framed_test("verify_cleanup_child", source, b"", None)
    final_state = after_cleanup["stdout_preview"].strip()
    assert final_state in ("-", "Z"), (pid, final_state)
    return {"name": "attach_disconnect_" + signal.Signals(signum).name,
            "kill_on_detach": True, "child_new_session": isolate, "child_pid": pid,
            "child_state_after_disconnect": disconnect_state,
            "child_state_after_terminal_kill": terminal_kill_state,
            "child_state_after_guarded_cleanup": final_state}


def main():
    results = [direct_test(), direct_test(echo=True)]
    payload = bytes(range(256)) * 4096
    echo = "import os; assert not any(os.isatty(i) for i in (0,1,2)); " \
           "exec('while True:\\n b=os.read(0,8192)\\n if not b: break\\n os.write(1,b)')"
    results.append(framed_test("all_bytes_1mib_echo", echo, payload, payload))
    duplex = "import os,threading; a=bytes(range(255,-1,-1))*4096; " \
             "t=threading.Thread(target=lambda: os.write(1,a)); t.start(); " \
             "b=__import__('sys').stdin.buffer.read(); t.join(); os.write(1,b); os.write(2,b'only-stderr'); raise SystemExit(37)"
    results.append(framed_test("duplex_eof_exit37_slow_reader", duplex, payload,
                               bytes(range(255,-1,-1)) * 4096 + payload, b"only-stderr", 37, pause=1))
    results.append(framed_test("stdin_half_close", "import sys; b=sys.stdin.buffer.read(); "
                               "sys.stdout.buffer.write(b'AFTER_EOF:'+b)", b"input", b"AFTER_EOF:input"))
    results.append(framed_test("cancel_child_group", "import time; time.sleep(120)",
                               b"", b"", expected_rc=-signal.SIGTERM, cancel=True))
    discovery = ('p=$(command -v herdr-core) || p=$HOME/.local/bin/herdr-core; '
                 '[ -x "$p" ] || exit 127; exec "$p" --version')
    version_source = "import subprocess,sys; p=subprocess.run(['/bin/sh','-c'," + repr(discovery) + "],capture_output=True); " \
                     "sys.stdout.buffer.write(p.stdout); sys.stderr.buffer.write(p.stderr); raise SystemExit(p.returncode)"
    results.append(framed_test("real_core_discovery_version", version_source, b"", b"hc 0.1.21\n"))
    results.extend(disconnect_test(signum, isolate=isolate)
                   for isolate in (True, False) for signum in (signal.SIGTERM, signal.SIGKILL))
    print(json.dumps({"alias": "default", "poolab_version": "0.8.2", "results": results}, indent=2))


if __name__ == "__main__":
    main()
