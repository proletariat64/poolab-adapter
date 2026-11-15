"""Opt-in co.bot regression tests; never register or control a Core session."""
import argparse
import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from transport_probe import framed_test


def process_stopped(pid):
    stat = Path("/proc") / str(pid) / "stat"
    return not stat.exists() or stat.read_text().split()[2] == "Z"


def owner_death_test(signum):
    module_dir = str(Path(__file__).resolve().parent)
    worker = """
import json, signal, sys, time
sys.path.insert(0, MODULE_DIR)
from transport_probe import Channel, python_command
channel = None
def terminate(*_):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)
try:
    channel = Channel(python_command('import time; time.sleep(120)'),
                      lease_seconds=2, heartbeat_interval=0.2)
    kind, data = channel.frame(time.monotonic()+15, startup=True)
    assert kind == b'R'
    print(json.dumps(dict(ready=json.loads(data), terminal=channel.terminal,
                          local_cli_pid=channel.process.pid)), flush=True)
    while True:
        signal.pause()
finally:
    if channel is not None:
        channel.close()
""".replace("MODULE_DIR", repr(module_dir))
    owner = subprocess.Popen(
        [sys.executable, "-u", "-c", worker], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    metadata = None
    try:
        assert owner.stdout is not None
        if not select.select([owner.stdout], [], [], 20)[0]:
            raise TimeoutError("test owner did not become ready")
        metadata = json.loads(owner.stdout.readline())
        started = time.monotonic()
        os.kill(owner.pid, signum)
        # Channel.close allows 6s for the CLI, 2s for its writer, and 20s for terminal kill.
        owner.wait(timeout=35)
        deadline = time.monotonic() + 3
        while not process_stopped(metadata["local_cli_pid"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process_stopped(metadata["local_cli_pid"]), "local attach survived owner death"
        time.sleep(3)
        remote_pids = [metadata["ready"]["pid"], metadata["ready"]["helper_pid"]]
        source = ("from pathlib import Path\nfor pid in " + repr(remote_pids)
                  + ":\n p=Path('/proc/'+str(pid)+'/stat')\n"
                  " if p.exists() and p.read_text().split()[2]!='Z': print(pid)\n")
        framed_test("verify_owner_death_remote_cleanup", source, b"", b"")
        return {"name": "owner_death_" + signal.Signals(signum).name, "passed": True,
                "owner_pid": owner.pid, **metadata,
                "verification_seconds": round(time.monotonic() - started, 3),
                "local_cli_stopped": True, "remote_helper_and_child_stopped": True}
    finally:
        if owner.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                owner.terminate()
            try:
                owner.wait(timeout=25)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.wait(timeout=3)
        try:
            if metadata is not None:
                subprocess.run(
                    ["poolab", "terminal", "kill", "--alias", "default", metadata["terminal"]],
                    check=True, capture_output=True, timeout=20,
                )
                if not process_stopped(metadata["local_cli_pid"]):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(metadata["local_cli_pid"], signal.SIGKILL)
        finally:
            for stream in (owner.stdout, owner.stderr):
                if stream is not None:
                    stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--pause", type=float, default=5)
    parser.add_argument("--baseline", action="store_true", help="also reproduce the old unbounded-output failure")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 20 or not 0 <= args.pause <= 10:
        parser.error("iterations must be 1..20 and pause must be 0..10 seconds")
    results = {"status": "running", "poolab_version": "0.8.2", "input_bytes_per_iteration": 8388608,
               "output_bytes_per_iteration": 8388608, "results": []}

    def save():
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(results, indent=2) + "\n")

    payload = bytes(range(256)) * 32768
    expected = bytes(range(255, -1, -1)) * 32768
    source = ("import os,sys,threading; assert not any(os.isatty(i) for i in (0,1,2)); "
              "a=bytes(range(255,-1,-1))*32768; "
              "t=threading.Thread(target=lambda: (sys.stdout.buffer.write(a),sys.stdout.buffer.flush()));"
              "t.start();b=sys.stdin.buffer.read();t.join();"
              "assert b==bytes(range(256))*32768;os.write(2,b'input-ok');raise SystemExit(37)")
    try:
        if args.baseline:
            try:
                framed_test("unbounded_output_baseline", "import os;os.write(1,bytes(range(256))*32768)",
                            b"", payload, flow_control=False)
            except EOFError as error:
                results["baseline"] = {"unexpected_eof_reproduced": True, "error": str(error)[:300]}
            else:
                results["baseline"] = {"unexpected_eof_reproduced": False}
            save()
        for index in range(args.iterations):
            result = framed_test("bounded_duplex_8mib_" + str(index + 1), source,
                                 payload, expected, b"input-ok", 37, pause=args.pause)
            results["results"].append(result)
            save()
            print("Passed transfer " + str(index + 1) + "/" + str(args.iterations), file=sys.stderr, flush=True)
        for signum in (signal.SIGTERM, signal.SIGKILL):
            results["results"].append(owner_death_test(signum))
            save()
        results["status"] = "passed"
        save()
    except BaseException as error:
        results["status"] = "failed"
        results["error"] = type(error).__name__ + ": " + str(error)
        save()
        raise
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
