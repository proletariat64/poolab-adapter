#!/usr/bin/env python3
"""Live acceptance against an owned, fully isolated Core pair (never the default Core).

Only this smoke client interprets Core RPC. The adapter remains byte-transparent.
Protocol reference: herdrdev/herdr-core at c84d6e76156a6c2aac1da20efbe800f10acfe897.
Reports are private, ignored JSON; captured server output is never printed or saved.
"""

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = struct.Struct("<IBI")
LIMIT = 16 << 20
TIMEOUT = 90


def isolation_env(root: str, home: str, path: str) -> dict[str, str]:
    """Construct a whitelist, not an overlay on the caller's Core environment."""
    return {
        "HOME": home,
        "PATH": path,
        "XDG_CONFIG_HOME": root + "/c",
        "XDG_DATA_HOME": root + "/d",
        "XDG_STATE_HOME": root + "/s",
        "XDG_RUNTIME_DIR": root + "/r",
        "HC_SOCKET": root + "/r/c.sock",
        "HC_SESSION": Path(root).name,
    }


def remote_prefix(root: str) -> str:
    validate_remote_root(root)
    env = isolation_env(root, "", "")
    # Expand only HOME/PATH in the remote shell, then discard its entire environment.
    assignments = ['HOME="$HOME"', 'PATH="$PATH"']
    assignments += [shlex.quote(k + "=" + v) for k, v in env.items() if k not in ("HOME", "PATH")]
    return "env -i " + " ".join(assignments)


def validate_remote_root(root: str) -> None:
    if not re.fullmatch(r"/var/tmp/pa-[a-z0-9_]{6,16}", root):
        raise ValueError("refused remote scratch path")


def remote_python(code: str) -> str:
    return "python3 -c " + shlex.quote(code)


def core_command(*arguments: str) -> str:
    script = ('p=$(command -v herdr-core) || p=$HOME/.local/bin/herdr-core; '
              '[ -x "$p" ] || exit 127; exec "$p" ' + shlex.join(arguments))
    return "sh -c " + shlex.quote(script)


def create_remote_command(token: str) -> str:
    return remote_python(
        "import os,tempfile,pathlib; os.umask(0o077); "
        "p=pathlib.Path(tempfile.mkdtemp(prefix='pa-',dir='/var/tmp')); "
        f"(p/'owner').write_text({token!r}); "
        "[(p/x).mkdir(mode=0o700) for x in ('c','d','s','r')]; print(p)"
    )


def delete_remote_command(root: str, token: str) -> str:
    validate_remote_root(root)
    return remote_python(
        "import os,pathlib,shutil,stat; "
        f"p=pathlib.Path({root!r}); s=p.lstat(); "
        "assert p.parent==pathlib.Path('/var/tmp') and p.name.startswith('pa-'); "
        "assert stat.S_ISDIR(s.st_mode) and s.st_uid==os.getuid(); "
        "assert not (s.st_mode & 0o077); "
        f"assert (p/'owner').read_text()=={token!r}; shutil.rmtree(p)"
    )


def owned_pids(status: dict) -> list[int]:
    pids = [status["pid"], *(holder["pid"] for holder in status.get("holders", []))]
    if any(type(pid) is not int or pid <= 1 for pid in pids):
        raise ValueError("invalid owned Core process metadata")
    return sorted(set(pids))


def process_check_code(pids: list[int]) -> str:
    return (
        "from pathlib import Path\nimport time,sys\n"
        "deadline=time.monotonic()+3\n"
        "while True:\n alive=[]\n"
        f" for pid in {pids!r}:\n"
        "  try: state=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[0]\n"
        "  except (FileNotFoundError,ProcessLookupError): continue\n"
        "  if state!='Z': alive.append(pid)\n"
        " if not alive: break\n"
        " if time.monotonic()>deadline: sys.exit(1)\n"
        " time.sleep(.05)\n"
    )


def marker_command(marker: str) -> str:
    """The literal marker must not occur in terminal command echo."""
    encoded = "".join(f"\\{b:03o}" for b in marker.encode("ascii"))
    return "printf '" + encoded + "\\n'"


def wrapper_source(adapter: str, alias: str, root: str) -> str:
    """Core appends exactly one remote shell line to its --via command."""
    prefix = remote_prefix(root)
    return (
        f"#!{sys.executable}\n"
        "import os, shlex, sys\n"
        "if len(sys.argv) != 2: raise SystemExit(64)\n"
        f"command = {prefix!r} + ' sh -c ' + shlex.quote(sys.argv[1])\n"
        f"os.execv({adapter!r}, [{adapter!r}, '--alias', {alias!r}, '--', command])\n"
    )


def current_artifact(adapter: Path) -> dict:
    """Compare packaged sources directly: reject stale builds without new fingerprints."""
    with zipfile.ZipFile(adapter) as archive:
        packaged = {n for n in archive.namelist()
                    if n.startswith("poolab_adapter/") and not n.endswith("/")}
        sources = {
            p.relative_to(ROOT).as_posix(): p
            for p in (ROOT / "poolab_adapter").rglob("*.py")
            if "__pycache__" not in p.parts
        }
        if packaged != set(sources):
            raise ValueError("artifact source set differs from worktree")
        if any(archive.read(name) != path.read_bytes() for name, path in sources.items()):
            raise ValueError("artifact is stale; rebuild it from this worktree")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    return {"git_head": head, "dirty": dirty, "authoritative": not dirty}


async def run(argv: list[str], *, env=None, data: bytes = b"", expected=0) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), TIMEOUT)
        if proc.returncode != expected:
            raise RuntimeError("command exit status did not match")
        return out
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


class CoreClient:
    """Bounded smoke-only framed RPC client; stream bytes are acknowledged as read."""

    def __init__(self, proc):
        self.proc = proc
        self.framed = False
        self.next_id = 0
        self.stream = bytearray()
        self.stderr_task = asyncio.create_task(self.discard_stderr())

    async def discard_stderr(self):
        while await self.proc.stderr.read(65536):
            pass

    async def exact(self, n: int) -> bytes:
        return await self.proc.stdout.readexactly(n)

    async def frame(self, channel: int, kind: int, payload: bytes):
        self.proc.stdin.write(HEADER.pack(channel, kind, len(payload)) + payload)
        await self.proc.stdin.drain()

    async def receive(self):
        channel, kind, size = HEADER.unpack(await self.exact(HEADER.size))
        if size > LIMIT:
            raise ValueError("oversized Core frame")
        payload = await self.exact(size)
        if channel and kind == 0:
            self.stream.extend(payload)
            if len(self.stream) > 4 << 20:
                del self.stream[:len(self.stream) - (4 << 20)]
            await self.frame(channel, 1, struct.pack("<I", size))
        return channel, kind, payload

    async def call(self, method: str, params: dict) -> dict:
        async def exchange():
            self.next_id += 1
            parameters = {"generation": 1, **params} if method == "hello" else params
            request = json.dumps({"jsonrpc": "2.0", "id": self.next_id,
                                  "method": method, "params": parameters}).encode()
            if self.framed:
                await self.frame(0, 0, request)
            else:
                self.proc.stdin.write(request + b"\n")
                await self.proc.stdin.drain()
            while True:
                if self.framed:
                    channel, kind, payload = await self.receive()
                    if channel or kind != 0:
                        continue
                else:
                    payload = await self.proc.stdout.readline()
                    if not payload or len(payload) > LIMIT:
                        raise RuntimeError("missing Core reply")
                reply = json.loads(payload)
                if reply.get("id") != self.next_id:
                    continue
                if "error" in reply:
                    raise RuntimeError("Core RPC failed: " + method)
                result = reply["result"]
                if method == "hello" and params.get("framed"):
                    if result.get("framed") is not True:
                        raise RuntimeError("Core did not negotiate framed mode")
                    self.framed = True
                return result
        return await asyncio.wait_for(exchange(), TIMEOUT)

    async def close(self, force=False):
        # Kill only this owned adapter, never a Core or any process found by name.
        if self.proc.returncode is None:
            if force:
                self.proc.kill()
            else:
                self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), 10)
        except TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        finally:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)


class Smoke:
    def __init__(self, adapter: Path, alias: str, report: dict):
        self.adapter = str(adapter)
        self.alias = alias
        self.report = report
        self.remote = None
        self.local = None
        self.local_env = None
        self.hc = None
        self.client = None
        self.remote_started = False
        self.local_started = False
        self.machine_attempted = False
        self.terminal_attempted = False
        self.terminal_name = "pa-" + uuid.uuid4().hex[:12]
        self.terminal = None
        self.terminal_pid = None
        self.remote_pids = []
        self.local_pids = []
        self.token = uuid.uuid4().hex

    def argv(self, command: str) -> list[str]:
        return [self.adapter, "--alias", self.alias, "--", command]

    async def remote_run(self, command: str) -> bytes:
        return await run(self.argv(command))

    def isolated(self, command: str) -> str:
        if self.remote is None:
            raise RuntimeError("remote isolation is not prepared")
        return remote_prefix(self.remote) + " " + command

    async def connect(self):
        remote = self.remote
        if remote is None:
            raise RuntimeError("remote isolation is not prepared")
        self.remote_started = True  # Even a partial startup needs isolated cleanup.
        proc = await asyncio.create_subprocess_exec(
            *self.argv(self.isolated(core_command("connect", "--session", Path(remote).name))),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=LIMIT + 1,
        )
        self.client = CoreClient(proc)
        return self.client

    async def marker(self, client, marker: str, raw_channel=None):
        if raw_channel is None:
            await client.call("terminal.send", {"terminal": self.terminal,
                                               "text": marker_command(marker), "force": True})
        else:
            await client.frame(raw_channel, 0, marker_command(marker).encode("ascii") + b"\r")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            read = await client.call("terminal.read", {"terminal": self.terminal, "lines": 100})
            if marker in read["text"] and marker.encode() in client.stream:
                return
            # The next RPC also drains pending attach frames, independently of polling.
            await asyncio.sleep(0.2)
        raise RuntimeError("marker missing from terminal read or attach stream")

    async def exercise(self):
        self.report["stage"] = "binary-duplex"
        # Output precedes input: communicate must drain while writing, then send EOF.
        payload = bytes(range(256)) * 4096
        code = (
            "import sys,threading; b=bytes(range(256))*4096; "
            "t=threading.Thread(target=lambda:(sys.stderr.buffer.write(b[::-1]),sys.stderr.buffer.flush()));"
            "t.start();sys.stdout.buffer.write(b);sys.stdout.buffer.flush();"
            "d=sys.stdin.buffer.read();sys.stdout.buffer.write(d);"
            "sys.stdout.buffer.flush();t.join();sys.exit(37)"
        )
        proc = await asyncio.create_subprocess_exec(
            *self.argv(remote_python(code)), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(payload), TIMEOUT)
            if (out, err, proc.returncode) != (payload * 2, payload[::-1], 37):
                raise RuntimeError("duplex/EOF/stderr/exit contract mismatch")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        self.report["checks"].append("binary-duplex-eof-stderr-exit37")

        self.report["stage"] = "remote-scratch"
        root = (await self.remote_run(create_remote_command(self.token))).decode().strip()
        validate_remote_root(root)
        self.remote = root
        self.report["remote_scratch"] = root
        self.report["stage"] = "remote-version"
        version = await self.remote_run(self.isolated(core_command("--version")))
        if not re.fullmatch(rb"(?:hc|herdr-core) [0-9]+\.[0-9]+\.[0-9]+[^\r\n]*\s*", version):
            raise RuntimeError("unexpected remote Core version")
        self.report["checks"].append("remote-core-version")

        self.report["stage"] = "remote-core"
        client = await self.connect()
        plain = await client.call("hello", {})
        core_id = plain["core"]["id"]
        if not core_id:
            raise RuntimeError("missing Core identity")
        framed = await client.call("hello", {"framed": True})
        if framed["core"]["id"] != core_id:
            raise RuntimeError("Core identity changed during hello")
        self.terminal_attempted = True
        created = await client.call("terminal.run", {"argv": ["/bin/sh"], "name": self.terminal_name,
                                                    "no_parent": True, "no_notify": True})
        self.terminal = created["address"]
        self.terminal_pid = created["pid"]
        if not self.terminal_pid:
            raise RuntimeError("owned terminal did not start")
        attached = await client.call("terminal.attach", {"terminal": self.terminal,
                                                         "cols": 100, "rows": 30})
        if not isinstance(attached["channel"], int) or attached["channel"] <= 0:
            raise RuntimeError("invalid attach stream channel")
        await self.marker(client, "PA_FIRST_" + self.token)
        await client.close(force=True)
        self.client = None
        client = await self.connect()
        again = await client.call("hello", {"framed": True})
        if again["core"]["id"] != core_id:
            raise RuntimeError("reconnect reached a different Core")
        existing = await client.call("terminal.get", {"terminal": self.terminal})
        if existing["address"] != self.terminal or existing["pid"] != self.terminal_pid:
            raise RuntimeError("reconnect changed the owned terminal")
        attached = await client.call("terminal.attach", {"terminal": self.terminal, "cols": 100,
                                                          "rows": 30, "resumed": True})
        channel = attached["channel"]
        if type(channel) is not int or channel <= 0:
            raise RuntimeError("invalid reconnect attach channel")
        await self.marker(client, "PA_SECOND_" + self.token, raw_channel=channel)
        self.remote_pids = owned_pids(await client.call("server.status", {}))
        self.report["owned_remote_pids"] = self.remote_pids
        self.report["checks"].append("plain-framed-attach-raw-input-reconnect-after-sigkill")

        self.report["stage"] = "local-via-registration"
        self.hc = shutil.which("hc") or shutil.which("herdr-core")
        if not self.hc:
            raise RuntimeError("installed local hc or herdr-core required")
        self.local = Path(tempfile.mkdtemp(prefix="pa-", dir="/tmp"))
        self.local.chmod(0o700)
        self.report["local_scratch"] = str(self.local)
        (self.local / "owner").write_text(self.token)
        for name in ("c", "d", "s", "r"):
            (self.local / name).mkdir(mode=0o700)
        self.local_env = isolation_env(str(self.local), os.environ["HOME"], os.environ["PATH"])
        wrapper = self.local / "via"
        wrapper.write_text(wrapper_source(self.adapter, self.alias, self.remote))
        wrapper.chmod(0o700)
        self.local_started = self.machine_attempted = True
        await run([self.hc, "machine", "add", "--name", "uatremote", "--via", str(wrapper),
                   "--session", Path(self.remote).name, "--temp", "--json"], env=self.local_env)
        listing = json.loads(await run([self.hc, "machine", "ls", "--json"], env=self.local_env))
        machines = listing["machines"]
        if len(machines) != 1 or machines[0]["name"] != "uatremote":
            raise RuntimeError("isolated machine registry mismatch")
        if machines[0]["id"] != core_id or machines[0].get("session") != Path(self.remote).name:
            raise RuntimeError("registered machine identity/session mismatch")
        self.local_pids = owned_pids(json.loads(await run(
            [self.hc, "server", "status", "--json"], env=self.local_env)))
        self.report["owned_local_pids"] = self.local_pids
        self.report["checks"].append("local-core-via-registration")

    async def cleanup(self, passed: bool):
        errors = []
        local, local_env, hc, remote = self.local, self.local_env, self.hc, self.remote

        async def attempt(label, operation):
            try:
                await operation()
                self.report["cleanup"][label] = "pass"
                return True
            except Exception as exc:
                errors.append(label)
                self.report["cleanup"][label] = type(exc).__name__
                return False

        if self.local_started and local is not None and local_env is not None and hc is not None:
            if self.machine_attempted:
                async def remove_machine():
                    # A failed add may still have registered it: inspect this isolated registry only.
                    listing = json.loads(await run([hc, "machine", "ls", "--json"],
                                                   env=local_env))
                    if any(m["name"] == "uatremote" for m in listing["machines"]):
                        await run([hc, "machine", "rm", "uatremote"], env=local_env)
                await attempt("local-machine-remove", remove_machine)
            async def stop_local():
                await run([hc, "server", "stop"], env=local_env)
                assert_socket_stopped(local / "r" / "c.sock")
                await run([sys.executable, "-c", process_check_code(self.local_pids)])
            await attempt("local-core-stop", stop_local)
        elif self.local_started:
            errors.append("local-isolation-missing")
            self.report["cleanup"]["local-isolation-missing"] = "refused unsafe cleanup"
        if self.remote_started and self.terminal_attempted and remote is not None:
            async def remove_terminal():
                client = self.client or await self.connect()
                # Cleanup reconnects may start plain; negotiate explicitly before RPC use.
                if not client.framed:
                    await client.call("hello", {"framed": True})
                listing = await client.call("terminal.list", {"all": True})
                owned = [t for t in listing["terminals"]
                         if t.get("name") == self.terminal_name
                         and (self.terminal is None or t.get("address") == self.terminal)]
                if not owned:
                    return
                if len(owned) != 1:
                    raise RuntimeError("ambiguous owned terminal")
                self.terminal = owned[0]["address"]
                await client.call("terminal.kill", {"terminal": self.terminal})
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    terminal = await client.call("terminal.get", {"terminal": self.terminal})
                    if terminal.get("state") == "finished":
                        await client.call("terminal.rm", {"terminal": self.terminal})
                        return
                    await asyncio.sleep(0.2)
                raise RuntimeError("owned terminal did not finish")
            await attempt("remote-terminal-remove", remove_terminal)
        if self.client:
            await attempt("adapter-close", self.client.close)
            self.client = None
        if self.remote_started and remote is not None:
            async def stop_remote():
                await self.remote_run(self.isolated(core_command("server", "stop")))
                # Prove stop without a command that could autostart a Core.
                code = (
                    "import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(2); "
                    f"r=s.connect_ex({(remote + '/r/c.sock')!r}); s.close(); "
                    "sys.exit(1 if r==0 else 0)"
                )
                await self.remote_run(remote_python(code))
                await self.remote_run(remote_python(process_check_code(self.remote_pids)))
            await attempt("remote-core-stop", stop_remote)
        elif self.remote_started:
            errors.append("remote-isolation-missing")
            self.report["cleanup"]["remote-isolation-missing"] = "refused unsafe cleanup"
        # Preserve private evidence on any failure; never remove a live/uncertain Core root.
        if passed and not errors:
            if remote is not None:
                await attempt("remote-scratch-remove", lambda: self.remote_run(
                    delete_remote_command(remote, self.token)))
            if local is not None and not errors:
                async def remove_local():
                    guarded_local_remove(local, self.token)
                await attempt("local-scratch-remove", remove_local)
        else:
            self.report["cleanup"]["evidence"] = "scratch directories preserved"
        if errors:
            raise RuntimeError("cleanup failed")


def assert_socket_stopped(path: Path):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(2)
        if sock.connect_ex(str(path)) == 0:
            raise RuntimeError("owned Core socket still accepts connections")


def guarded_local_remove(root: Path, token: str):
    metadata = root.lstat()
    if (root.parent != Path("/tmp") or not re.fullmatch(r"pa-[a-z0-9_]{6,16}", root.name)
            or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077 or (root / "owner").read_text() != token):
        raise ValueError("refused local scratch deletion")
    shutil.rmtree(root)


async def execute(args, report: dict) -> int:
    smoke = Smoke(args.adapter.resolve(), args.alias, report)
    passed = False
    try:
        report["stage"] = "artifact-preflight"
        report.update(current_artifact(args.adapter.resolve()))
        version = (await run([str(args.adapter.resolve()), "--version"])).decode().strip()
        if not re.fullmatch(r"herdr-poolab \d+\.\d+\.\d+[-+.\w]*", version):
            raise RuntimeError("unexpected artifact version")
        report["artifact_version"] = version
        await smoke.exercise()
        passed = True
    except Exception as exc:
        report["failure"] = {"stage": report["stage"], "type": type(exc).__name__}
    finally:
        try:
            await smoke.cleanup(passed)
        except Exception as exc:
            report["cleanup_failure"] = type(exc).__name__
            passed = False
    report["status"] = "pass" if passed else "fail"
    return 0 if passed else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, default=ROOT / "dist/herdr-poolab")
    parser.add_argument("--alias", default=os.environ.get("POOLAB_ALIAS", "default"))
    parser.add_argument("--output", type=Path, default=ROOT / ".local/smoke.json")
    args = parser.parse_args(argv)
    # Reports must not accidentally become public repo content.
    if args.output.is_symlink():
        parser.error("--output must not be a symlink")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT.resolve()):
        parser.error("--output must stay inside this worktree")
    ignored = subprocess.run(["git", "check-ignore", "--quiet", str(output)], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if ignored.returncode != 0:
        parser.error("--output must be a Git-ignored path in this worktree")
    def terminate(_number, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    os.umask(0o077)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "fail", "checks": [], "cleanup": {}, "started_at": int(time.time())}
    try:
        code = asyncio.run(execute(args, report))
    except KeyboardInterrupt:
        report["failure"] = {"stage": report.get("stage"), "type": "KeyboardInterrupt"}
        code = 1
    finally:
        report["finished_at"] = int(time.time())
        # Keep prior pass/failure evidence across repeated UAT and review cycles.
        if output.exists():
            previous = output.with_name(output.stem + "-previous-" + uuid.uuid4().hex[:8] + ".json")
            output.replace(previous)
        # Open without following symlinks; evidence must stay private even on reruns.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(report, handle, indent=2)
            handle.write("\n")
    print(f"smoke: {report['status']}; private evidence: {output}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
