"""Standalone adapter acceptance at the executable process boundary."""

import contextlib
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AdapterExecutableTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "state"
        self.state.mkdir()
        backend = Path(self.directory.name) / "poolab"
        shutil.copyfile(ROOT / "tests" / "fake_poolab.py", backend)
        backend.chmod(0o755)
        self.env = dict(os.environ, PATH=str(backend.parent) + os.pathsep + os.environ["PATH"],
                        FAKE_POOLAB_STATE=str(self.state))
        self.env.pop("PYTHONPATH", None)
        self.addCleanup(self.cleanup_carriers)

    def cleanup_carriers(self):
        for path in self.state.glob("term-*.json"):
            item = json.loads(path.read_text())
            if item.get("pid"):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(item["pid"], 9)
        deadline = time.monotonic() + 3
        while list(self.state.glob("term-*.json")) and time.monotonic() < deadline:
            time.sleep(0.02)

    def invoke(self, command, data=b"", timeout=20):
        return subprocess.run(
            [sys.executable, "-m", "poolab_adapter", "--alias", "fixture", "--", command],
            input=data, cwd=ROOT, env=self.env, capture_output=True, check=False, timeout=timeout,
        )

    def test_binary_echo_stderr_and_remote_exit(self):
        data = bytes(range(256)) * 128
        source = ("import sys;sys.stdout.buffer.write(sys.stdin.buffer.read());"
                  "sys.stderr.write('remote-only\\n');raise SystemExit(37)")
        result = self.invoke("exec python3 -c " + shlex.quote(source), data)
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(result.stdout, data)
        self.assertEqual(result.stderr, b"remote-only\n")
        self.assertFalse(list(self.state.glob("term-*.json")), "owned terminal was not removed")

    def test_child_exit_does_not_wait_for_a_descendant_holding_stdin(self):
        source = ("import subprocess,sys;subprocess.Popen([sys.executable,'-c',"
                  "'import time;time.sleep(10)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                  "print('done',flush=True)")
        result = self.invoke("exec python3 -c " + shlex.quote(source), b"x" * 524288, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"done\n")
        self.assertFalse(list(self.state.glob("term-*.json")))

    def test_early_input_close_preserves_final_output(self):
        source = "import os,sys;os.close(0);sys.stdout.buffer.write(b'z'*262144)"
        result = self.invoke("exec python3 -c " + shlex.quote(source), b"x" * 1048576)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"z" * 262144)
        self.assertEqual(result.stderr, b"")

    def test_remote_shell_quoting_is_preserved(self):
        arguments = ["two words", "a'b", "$HOME", ";printf injected"]
        source = "import sys,json;print(json.dumps(sys.argv[1:]))"
        command = "exec python3 -c " + shlex.quote(source) + " " + " ".join(map(shlex.quote, arguments))
        result = self.invoke(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)

    def test_remote_missing_executable_status_is_preserved(self):
        result = self.invoke("exec poolab-adapter-nonexistent-program")
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"not found", result.stderr)

    def test_invalid_final_record_is_never_success(self):
        for fault in ("truncate-final", "wrong-counter"):
            with self.subTest(fault=fault):
                self.env["FAKE_CARRIER_FAULT"] = fault
                result = self.invoke("printf tail")
                self.assertEqual(result.returncode, 255, result.stderr)
                self.assertNotIn(b"fake-backend-private-diagnostic", result.stdout + result.stderr)

    def test_startup_failure_reconciles_only_its_own_created_terminal(self):
        foreign = {"id": "term-foreign", "name": "unrelated-session", "pid": None}
        path = self.state / "term-foreign.json"
        path.write_text(json.dumps(foreign))
        self.env["FAKE_CREATE_DELAY"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "poolab_adapter", "--startup-timeout", "0.3", "--", "printf no"],
            cwd=ROOT, env=self.env, input=b"", capture_output=True, check=False, timeout=8,
        )
        self.assertEqual(result.returncode, 255)
        self.assertEqual(json.loads(path.read_text()), foreign)
        self.assertEqual(list(self.state.glob("term-*.json")), [path])
        events = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        created = {event["id"] for event in events if event["action"] == "create"}
        killed = {event["id"] for event in events if event["action"] == "kill"}
        self.assertEqual(created, killed)
        path.unlink()

    def test_bootstrap_handles_a_command_larger_than_a_canonical_tty_line(self):
        randomizer = random.Random(42)
        comment = "".join(randomizer.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=12000))
        result = self.invoke("printf long; #" + comment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"long")

    def test_owner_death_during_creation_does_not_leave_a_terminal(self):
        self.env["FAKE_CREATE_DELAY"] = "0.5"
        process = subprocess.Popen(
            [sys.executable, "-m", "poolab_adapter", "--", "sleep 60"], cwd=ROOT, env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 4
            while not list(self.state.glob("term-*.json")) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(list(self.state.glob("term-*.json")), "creation never started")
            process.kill()
            process.communicate(timeout=3)
            deadline = time.monotonic() + 4
            while list(self.state.glob("term-*.json")) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(list(self.state.glob("term-*.json")), "startup resource owner lost cleanup")
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)

    def test_closed_local_output_cancels_owned_work(self):
        source = "import sys;sys.stdout.buffer.write(b'x'*1048576)"
        process = subprocess.Popen(
            [sys.executable, "-m", "poolab_adapter", "--", "exec python3 -c " + shlex.quote(source)],
            cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        process.stdout.close()
        try:
            self.assertEqual(process.wait(timeout=6), 141)
            self.assertFalse(list(self.state.glob("term-*.json")))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            process.stderr.close()

    def test_shared_output_pipe_retains_its_original_blocking_flag(self):
        for blocking in (True, False):
            with self.subTest(blocking=blocking):
                read_fd, write_fd = os.pipe()
                with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb", buffering=0) as writer:
                    os.set_blocking(writer.fileno(), blocking)
                    result = subprocess.run(
                        [sys.executable, "-m", "poolab_adapter", "--", "printf out; printf err >&2"],
                        cwd=ROOT, env=self.env, input=b"", stdout=writer, stderr=subprocess.STDOUT,
                        check=False, timeout=8,
                    )
                    restored = os.get_blocking(writer.fileno())
                    writer.close()
                    output = reader.read()
                self.assertEqual(result.returncode, 0, output)
                self.assertEqual(restored, blocking, "adapter changed a shared output description")
                self.assertEqual(len(output), 6)
                self.assertIn(b"out", output)
                self.assertIn(b"err", output)

    def test_packaged_adapter_runs_outside_the_checkout(self):
        artifact = Path(self.directory.name) / "binary directory" / "herdr-poolab"
        built = subprocess.run(
            [sys.executable, "scripts/package.py", "--output", str(artifact)], cwd=ROOT,
            capture_output=True, check=False, timeout=10,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        environment = {**self.env, "PATH": "." + os.pathsep + os.environ["PATH"]}
        result = subprocess.run(
            [str(artifact), "--alias", "fixture", "--", "printf packaged"],
            cwd=self.directory.name, env=environment, input=b"", capture_output=True,
            check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"packaged")
        self.assertEqual(result.stderr, b"")
        self.assertFalse(list(self.state.glob("term-*.json")))

    def test_help_and_version_need_no_poolab_authentication(self):
        for option in ("--help", "--version"):
            with self.subTest(option=option):
                result = subprocess.run(
                    [sys.executable, "-m", "poolab_adapter", option], cwd=ROOT,
                    capture_output=True, check=False, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b"herdr-poolab", result.stdout)
                self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
