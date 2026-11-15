"""Self-contained remote half of the private PA2 carrier protocol."""

import base64
import json
import math
import os
import queue
import selectors
import signal
import subprocess
import sys
import threading
import time
import tty

CHUNK = 4096
SLOTS = 16
MAX_FRAME = 8192


def run(nonce, command, lease_seconds=30):
    """Run one command in this helper's owned foreground process group.

    The caller must allocate a private PTY and group. Successful acknowledgment,
    protocol failure, cancellation timeout, and lease expiry kill that group,
    including this helper; this function does not return on normal completion.
    """
    token = nonce.encode("ascii")
    if not token or len(token) > 128 or any(c < 33 or c > 126 or c == 58 for c in token):
        raise ValueError("invalid carrier nonce")
    if not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("invalid lease duration")
    prefix = b"PA2:" + token + b":"
    group = os.getpgrp()
    if os.tcgetpgrp(0) != group:
        raise RuntimeError("helper does not own the terminal foreground group")
    tty.setraw(0)
    condition = threading.Condition()
    output_lock = threading.Lock()
    credits = {b"I": SLOTS, b"O": SLOTS, b"E": SLOTS}
    counts = {"stdin": 0, "stdout": 0, "stderr": 0}
    input_queue = queue.Queue(maxsize=SLOTS + 1)
    stdin_gone = threading.Event()
    writer_done = threading.Event()
    exit_sent = threading.Event()
    exit_ack = threading.Event()
    last_liveness = time.monotonic()
    cancel_started = None

    def kill_group():
        # Foreground ownership was checked before the child was created. Never
        # target a PID discovered outside this invocation's group.
        try:
            os.killpg(group, signal.SIGKILL)
        finally:
            os._exit(124)

    def interrupted(signum, _frame):
        if signum == signal.SIGTERM and cancel_started is not None:
            return
        kill_group()

    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)

    def write_all(fd, data):
        while data:
            written = os.write(fd, data)
            if written <= 0:
                raise OSError("carrier write made no progress")
            data = data[written:]

    def frame(kind, data=b""):
        wire = prefix + kind + b":" + data + b"\n"
        if len(wire) > MAX_FRAME:
            raise ValueError("frame exceeds wire limit")
        with output_lock:
            write_all(1, wire)

    def watchdog():
        # This thread never takes the carrier lock or touches child pipes.
        while True:
            time.sleep(min(0.1, lease_seconds / 4))
            with condition:
                now = time.monotonic()
                expired = now - last_liveness >= lease_seconds
                cancelled = cancel_started is not None and now - cancel_started >= 2
            if expired or cancelled:
                kill_group()

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        child = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=os.path.expanduser("~"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        child_input = child.stdin
        child_output = child.stdout
        child_error = child.stderr
        assert child_input is not None and child_output is not None and child_error is not None
        os.set_blocking(child_input.fileno(), False)

        def input_writer():
            try:
                while True:
                    try:
                        data = input_queue.get(timeout=0.1)
                    except queue.Empty:
                        if child.poll() is None:
                            continue
                        stdin_gone.set()
                        child_input.close()
                        frame(b"N")
                        return
                    if data is None:
                        child_input.close()
                        return
                    while data:
                        if child.poll() is not None:
                            raise BrokenPipeError("command stopped accepting input")
                        try:
                            written = os.write(child_input.fileno(), data)
                        except BlockingIOError:
                            with condition:
                                condition.wait(0.02)
                            continue
                        if written <= 0:
                            raise OSError("child stdin write made no progress")
                        with condition:
                            counts["stdin"] += written
                        data = data[written:]
                    # One credit per delivered FRAME, not per byte. Publish the
                    # allowance before the peer can observe its credit record.
                    with condition:
                        credits[b"I"] += 1
                    frame(b"C", b"I:1")
            except BrokenPipeError:
                stdin_gone.set()
                child_input.close()
                frame(b"N")
            except BaseException:
                kill_group()
            finally:
                writer_done.set()

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
                    if kind == b"D" and not input_closed:
                        decoded = base64.b64decode(data, validate=True)
                        if not 0 < len(decoded) <= CHUNK:
                            raise ValueError("invalid data size")
                        with condition:
                            if credits[b"I"] <= 0:
                                raise ValueError("stdin credit exceeded")
                            credits[b"I"] -= 1
                        # N revokes future credits but permits the bounded set
                        # already on the wire to be discarded without blocking.
                        if not stdin_gone.is_set():
                            input_queue.put_nowait(decoded)
                    elif kind == b"F" and not input_closed and not data:
                        input_closed = True
                        if not stdin_gone.is_set():
                            input_queue.put_nowait(None)
                    elif kind == b"C":
                        stream, separator, count = data.partition(b":")
                        if (not separator or stream not in (b"O", b"E")
                                or not count.isdigit() or len(count) > 2):
                            raise ValueError("invalid output credit")
                        amount = int(count)
                        with condition:
                            if amount <= 0 or credits[stream] + amount > SLOTS:
                                raise ValueError("output credit exceeded")
                            credits[stream] += amount
                            condition.notify_all()
                    elif kind == b"H" and not data:
                        with condition:
                            last_liveness = time.monotonic()
                        frame(b"P")
                    elif kind == b"K" and not data:
                        with condition:
                            if cancel_started is None:
                                cancel_started = time.monotonic()
                                os.killpg(group, signal.SIGTERM)
                    elif kind == b"A" and exit_sent.is_set() and not data:
                        exit_ack.set()
                        return
                    else:
                        raise ValueError("invalid frame state")
                    with condition:
                        last_liveness = time.monotonic()
            except BaseException:
                kill_group()

        metadata = {
            "version": 2, "slots": SLOTS, "chunk": CHUNK,
            "pid": child.pid, "helper_pid": os.getpid(), "pgid": group,
            "lease_seconds": lease_seconds,
        }
        # A leading newline separates the carrier from terminal startup text.
        write_all(1, b"\n")
        frame(b"R", json.dumps(metadata).encode("ascii"))
        for target in (input_writer, input_loop):
            threading.Thread(target=target, daemon=True).start()
        selector = selectors.DefaultSelector()
        selector.register(child_output, selectors.EVENT_READ, (b"D", b"O", "stdout"))
        selector.register(child_error, selectors.EVENT_READ, (b"E", b"E", "stderr"))
        try:
            while selector.get_map():
                progressed = False
                for key, _ in selector.select(0.1):
                    kind, stream, counter = key.data
                    with condition:
                        if credits[stream] <= 0:
                            continue
                    data = os.read(key.fd, CHUNK)
                    progressed = True
                    if data:
                        with condition:
                            credits[stream] -= 1
                        frame(kind, base64.b64encode(data))
                        counts[counter] += len(data)
                    else:
                        frame(b"O" if stream == b"O" else b"S")
                        selector.unregister(key.fileobj)
                        (child_output if stream == b"O" else child_error).close()
                if not progressed:
                    with condition:
                        condition.wait(0.05)
            status = child.wait()
            # Freeze the accepted-write counter before publishing the result.
            # The independent lease/cancel watchdog still bounds a writer held
            # by descendants that inherited stdin after the child exited.
            writer_done.wait()
            with condition:
                result = dict(counts, status=status)
            exit_sent.set()
            frame(b"X", json.dumps(result).encode("ascii"))
            exit_ack.wait()
        finally:
            selector.close()
    finally:
        kill_group()
