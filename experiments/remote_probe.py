"""Ephemeral experimental relay; sent in memory, never installed remotely."""
import base64
import json
import os
import queue
import selectors
import signal
import subprocess
import sys
import threading
import time
import tty
from contextlib import suppress

CHUNK = 4096
WINDOW = 65536
MAX_FRAME = 8192


def write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def run(nonce, command, direct=False, isolate=False, flow_control=True, lease_seconds=30):
    tty.setraw(0)
    prefix = ("PA1:" + nonce + ":").encode()
    if direct:
        write_all(1, b"\n" + prefix + b"BEGIN\n")
        data = bytes(range(256)) * 16
        if direct == "echo":
            received = bytearray()
            while len(received) < len(data):
                chunk = os.read(0, len(data) - len(received))
                if not chunk:
                    raise EOFError("direct probe input closed")
                received.extend(chunk)
            data = bytes(received)
        write_all(1, data + prefix + b"END\n")
        os.read(0, 1)
        return

    group = os.getpgrp()
    if os.tcgetpgrp(0) != group:
        raise RuntimeError("helper does not own the terminal foreground group")
    child = subprocess.Popen(
        ["/bin/sh", "-c", command], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=isolate,
    )
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None
    child_input, child_output, child_error = child.stdin, child.stdout, child.stderr
    output_lock = threading.Lock()
    condition = threading.Condition()
    credits = {b"O": WINDOW, b"E": WINDOW, b"I": WINDOW}
    # A valid peer can use one-byte frames throughout its byte allowance, plus EOF.
    input_queue = queue.Queue(maxsize=WINDOW + 1)
    failure = threading.Event()
    finished = threading.Event()
    exit_ack = threading.Event()
    exit_sent = threading.Event()
    stdin_gone = threading.Event()
    last_liveness = time.monotonic()
    cancel_started = None

    def stop():
        failure.set()
        with suppress(ProcessLookupError):
            if isolate:
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
        with condition:
            condition.notify_all()

    def interrupted(signum, *_):
        if signum == signal.SIGTERM and cancel_started is not None:
            return  # Cancellation signals the owned group, but this helper must drain its pipes.
        stop()
        raise SystemExit(128)

    signal.signal(signal.SIGHUP, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)

    def frame(kind, data=b""):
        wire = prefix + kind + b":" + data + b"\n"
        if len(wire) > MAX_FRAME:
            raise ValueError("frame exceeds wire limit")
        with output_lock:
            write_all(1, wire)

    def watchdog():
        while not finished.wait(min(0.1, lease_seconds / 4)):
            with condition:
                now = time.monotonic()
                expired = (now - last_liveness > lease_seconds
                           or (cancel_started is not None and now - cancel_started > 2))
            if expired:
                if isolate:
                    with suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                else:
                    os.killpg(group, signal.SIGKILL)
                os._exit(124)

    def input_writer():
        pending_credit = 0
        try:
            while True:
                data = input_queue.get()
                if data is None:
                    child_input.close()
                    if flow_control and pending_credit:
                        frame(b"C", b"I:" + str(pending_credit).encode())
                    return
                write_all(child_input.fileno(), data)
                if flow_control:
                    with condition:
                        credits[b"I"] += len(data)
                    pending_credit += len(data)
                    # Do not turn tiny input frames into an unbounded burst of credit frames.
                    if pending_credit >= CHUNK:
                        frame(b"C", b"I:" + str(pending_credit).encode())
                        pending_credit = 0
        except BrokenPipeError:
            stdin_gone.set()
            frame(b"N")
            return
        except OSError:
            stop()

    def input_loop():
        nonlocal last_liveness, cancel_started
        input_closed = False
        try:
            while True:
                line = sys.stdin.buffer.readline(MAX_FRAME + 1)
                if (not line or len(line) > MAX_FRAME or not line.endswith(b"\n")
                        or not line.startswith(prefix)):
                    raise ValueError("invalid carrier input")
                kind, separator, data = line[len(prefix):-1].partition(b":")
                if not separator:
                    raise ValueError("missing frame separator")
                with condition:
                    last_liveness = time.monotonic()
                if kind == b"D" and not input_closed:
                    decoded = base64.b64decode(data, validate=True)
                    if not 0 < len(decoded) <= CHUNK:
                        raise ValueError("invalid data size")
                    with condition:
                        if flow_control and len(decoded) > credits[b"I"]:
                            raise ValueError("stdin credit exceeded")
                        credits[b"I"] -= len(decoded)
                    if not stdin_gone.is_set():
                        input_queue.put_nowait(decoded)
                elif kind == b"F" and not input_closed and not data:
                    input_closed = True
                    input_queue.put_nowait(None)
                elif kind == b"C" and flow_control:
                    stream, separator, count = data.partition(b":")
                    if not separator or stream not in (b"O", b"E"):
                        raise ValueError("invalid credit stream")
                    amount = int(count)
                    with condition:
                        if amount <= 0 or credits[stream] + amount > WINDOW:
                            raise ValueError("invalid credit amount")
                        credits[stream] += amount
                        condition.notify_all()
                elif kind == b"H" and not data:
                    pass
                elif kind == b"K" and not data:
                    with condition:
                        if cancel_started is None:
                            cancel_started = time.monotonic()
                    with suppress(ProcessLookupError):
                        os.killpg(child.pid if isolate else group, signal.SIGTERM)
                elif kind == b"A" and exit_sent.is_set() and not data:
                    exit_ack.set()
                    return
                else:
                    raise ValueError("invalid frame state")
        except (OSError, ValueError, queue.Full):
            stop()

    write_all(1, b"\n")
    frame(b"R", json.dumps({"pid": child.pid, "helper_pid": os.getpid(), "pgid": group,
                            "child_stdio": "pipes", "helper_tty": os.isatty(0),
                            "window": WINDOW if flow_control else 0,
                            "lease_seconds": lease_seconds}).encode())
    for target in (watchdog, input_writer, input_loop):
        threading.Thread(target=target, daemon=True).start()
    selector = selectors.DefaultSelector()
    selector.register(child_output, selectors.EVENT_READ, (b"D", b"O"))
    selector.register(child_error, selectors.EVENT_READ, (b"E", b"E"))
    try:
        while selector.get_map():
            if failure.is_set():
                raise RuntimeError("carrier input failed")
            progressed = False
            for key, _ in selector.select(0.1):
                kind, stream = key.data
                with condition:
                    budget = min(CHUNK, credits[stream]) if flow_control else CHUNK
                if not budget:
                    continue
                data = os.read(key.fd, budget)
                progressed = True
                if data:
                    if flow_control:
                        with condition:
                            credits[stream] -= len(data)
                    frame(kind, base64.b64encode(data))
                else:
                    frame(b"O" if kind == b"D" else b"S")
                    selector.unregister(key.fileobj)
                    (child_output if kind == b"D" else child_error).close()
            if not progressed:
                with condition:
                    condition.wait(0.05)
        while child.poll() is None:
            if failure.wait(0.05):
                raise RuntimeError("carrier input failed")
        exit_sent.set()
        frame(b"X", str(child.returncode).encode())
        while not exit_ack.wait(0.1):
            if failure.is_set():
                raise RuntimeError("carrier input failed before exit acknowledgment")
    finally:
        finished.set()
        stop()
        child.wait()
        selector.close()
        if not isolate:
            # The complete exit record was acknowledged, or the carrier failed.
            # End every remaining process in this owned terminal group, including this helper.
            os.killpg(group, signal.SIGKILL)
