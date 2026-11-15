"""Local Linux parent-death tests; no network access."""
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

GUARD = str(Path(__file__).resolve().parents[1] / "experiments" / "parent_exec.py")


class ParentExecTests(unittest.TestCase):
    def test_argv_is_not_interpreted_by_a_shell(self):
        arguments = ["two words", "quote'", "$HOME", ";exit 99"]
        result = subprocess.run(
            [sys.executable, GUARD, str(os.getpid()), sys.executable, "-c",
             "import json,sys;print(json.dumps(sys.argv[1:]))", *arguments],
            capture_output=True, check=True, timeout=5,
        )
        self.assertEqual(json.loads(result.stdout), arguments)

    def test_guard_rejects_parent_death_race(self):
        result = subprocess.run(
            [sys.executable, GUARD, "0", sys.executable, "-c", "raise SystemExit(77)"],
            capture_output=True, check=False, timeout=5,
        )
        self.assertEqual(result.returncode, 125)

    def test_child_stops_when_owner_is_killed(self):
        code = ("import subprocess,sys,os,time;"
                "p=subprocess.Popen([sys.executable," + repr(GUARD)
                + ",str(os.getpid()),sys.executable,'-u','-c',"
                "'import time;print(\"READY\",flush=True);time.sleep(60)'],"
                "stdout=subprocess.PIPE,start_new_session=True);"
                "assert p.stdout.readline()==b'READY\\n';print(p.pid,flush=True);"
                "sys.stdin.buffer.read()")
        owner = subprocess.Popen(
            [sys.executable, "-u", "-c", code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        child = None
        try:
            assert owner.stdout is not None
            child = int(owner.stdout.readline())
            owner.kill()
            owner.wait(timeout=3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                stat = Path("/proc") / str(child) / "stat"
                if not stat.exists() or stat.read_text().split()[2] == "Z":
                    return
                time.sleep(0.02)
            self.fail("child remained alive after owner SIGKILL")
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)
            if child is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child, signal.SIGKILL)
            for stream in (owner.stdin, owner.stdout, owner.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
