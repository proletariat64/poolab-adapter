"""Build offline, or publish a verified release from clean merged main."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from package import ROOT, build, package_version


class ReleaseError(RuntimeError):
    """An explicit release gate failed."""


def run(*args: str) -> str:
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)
    if result.returncode:
        # Do not echo provider stderr, which can contain authentication/environment data.
        raise ReleaseError(f"{args[0]} command failed (exit {result.returncode})")
    return result.stdout.strip()


def api(path: str, *, optional: bool = False, fields: tuple[str, ...] = ()) -> Any:
    command = ["gh", "api", path]
    if fields:
        command.extend(["--method", "POST"])
        for field in fields:
            command.extend(["-f", field])
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if optional and result.returncode and "(HTTP 404)" in result.stderr:
        return None
    if result.returncode:
        raise ReleaseError(f"GitHub API request failed: {path} (exit {result.returncode})")
    return json.loads(result.stdout)


def merged_head() -> str:
    if run("git", "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("release requires a clean working tree, including untracked files")
    if run("git", "branch", "--show-current") != "main":
        raise ReleaseError("release requires the merged main branch")
    if run("git", "rev-parse", "--abbrev-ref", "@{upstream}") != "origin/main":
        raise ReleaseError("main must track origin/main")
    run("git", "fetch", "--quiet", "origin", "main")
    head = run("git", "rev-parse", "HEAD")
    if head != run("git", "rev-parse", "origin/main"):
        raise ReleaseError("HEAD must equal origin/main exactly")
    remote = run("git", "ls-remote", "origin", "refs/heads/main").split()
    if not remote or remote[0] != head:
        raise ReleaseError("HEAD must equal the current remote main exactly")
    return head


def verify_ci(head: str, repository: str) -> None:
    runs = json.loads(
        run(
            "gh", "run", "list", "--repo", repository, "--workflow", "ci.yml", "--branch", "main",
            "--event", "push", "--commit", head, "--limit", "100",
            "--json", "databaseId,headSha,status,conclusion",
        )
    )
    for workflow in runs:
        if (
            workflow["headSha"] != head
            or workflow["status"] != "completed"
            or workflow["conclusion"] != "success"
        ):
            continue
        jobs = json.loads(
            run("gh", "run", "view", str(workflow["databaseId"]), "--repo", repository,
                "--json", "jobs")
        )["jobs"]
        required = {"check (3.11)", "check (3.12)", "doc-guard"}
        successful = {
            job["name"] for job in jobs
            if job["status"] == "completed" and job["conclusion"] == "success"
        }
        if required <= successful:
            return
    raise ReleaseError("HEAD needs successful ci.yml push checks: check (3.11/3.12), doc-guard")


def require_absent(repository: str, version: str) -> None:
    for path in (f"releases/tags/{version}", f"git/ref/tags/{version}"):
        if api(f"repos/{repository}/{path}", optional=True) is not None:
            raise ReleaseError(f"{version} already has a release or tag; refusing to overwrite")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="release tag, such as v0.1.0")
    parser.add_argument("--dry-run", action="store_true", help="offline build and version check only")
    args = parser.parse_args()
    try:
        expected = package_version()
        if args.version != f"v{expected}":
            raise ReleaseError(f"release version must match package version: v{expected}")
        head = None
        repository = None
        if not args.dry_run:
            head = merged_head()
            origin = run("git", "remote", "get-url", "origin")
            repository = json.loads(
                run("gh", "repo", "view", origin, "--json", "nameWithOwner")
            )["nameWithOwner"]
            require_absent(repository, args.version)
            verify_ci(head, repository)
            for target in ("check", "doc-guard", "smoke"):
                # Preserve useful local test output, but never print provider responses.
                result = subprocess.run(["make", target], cwd=ROOT, check=False)
                if result.returncode:
                    raise ReleaseError(f"make {target} failed (exit {result.returncode})")
            if merged_head() != head:
                raise ReleaseError("main changed during release checks")
        artifact = build(ROOT / "dist" / "herdr-poolab")
        reported = run(sys.executable, str(artifact), "--version")
        if reported not in {expected, f"v{expected}", f"herdr-poolab {expected}"}:
            raise ReleaseError(f"built executable did not report package version {expected}")
        if args.dry_run:
            print(f"Dry run: verified {args.version}: {artifact}; nothing published")
            return 0
        if not isinstance(repository, str) or not isinstance(head, str):
            raise ReleaseError("repository and merged HEAD must be resolved before publication")
        # Recheck immediately before the first remote mutation. Never replace a tag or asset.
        if merged_head() != head:
            raise ReleaseError("main changed while building the artifact")
        require_absent(repository, args.version)
        api(
            f"repos/{repository}/git/refs",
            fields=(f"ref=refs/tags/{args.version}", f"sha={head}"),
        )
        run(
            "gh", "release", "create", args.version, str(artifact), "--repo", repository,
            "--verify-tag",
            "--title", args.version, "--notes", f"Standalone poolab adapter {args.version}.",
        )
        release = api(f"repos/{repository}/releases/tags/{args.version}")
        tag = api(f"repos/{repository}/git/ref/tags/{args.version}")
        assets = api(f"repos/{repository}/releases/{release['id']}/assets?per_page=100")
        matching = [asset for asset in assets if asset["name"] == artifact.name]
        if (
            release["tag_name"] != args.version
            or release["draft"]
            or tag["object"]["sha"] != head
            or len(matching) != 1
            or matching[0]["size"] != artifact.stat().st_size
        ):
            raise ReleaseError("release/tag/asset verification failed; inspect GitHub before retrying")
        with tempfile.TemporaryDirectory(prefix="herdr-poolab-release-") as temporary:
            run(
                "gh", "release", "download", args.version, "--repo", repository,
                "--pattern", artifact.name,
                "--dir", temporary,
            )
            if (Path(temporary) / artifact.name).read_bytes() != artifact.read_bytes():
                raise ReleaseError("published asset differs from local executable")
        print(release["html_url"])
        return 0
    except (ReleaseError, OSError, ValueError, SyntaxError, KeyError, TypeError) as error:
        print(f"release: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
