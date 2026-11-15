# Operations and release

## Runtime boundary

The executable takes one complete remote shell command after its options. It
executes that command remotely, just as a custom Core transport must. The command
is authorized code, not a configuration file to evaluate locally. No SSH server,
new network listener, or separately copied authentication token is required.

poolab's installed CLI and existing account configuration remain authoritative.
Validate login separately if startup fails; the adapter never prompts. The
startup timeout defaults to 10 seconds, below Core's normal hello budget. The
remote liveness lease defaults to 30 seconds. Operators can adjust them with
`--startup-timeout` and `--lease-timeout`, both finite positive durations.

Only an exclusively created terminal is used. A short raw-mode loader completes
before the bundled helper payload is sent, avoiding canonical-terminal line
limits and raw-mode input flushing. The remote command itself receives ordinary
pipes, not a PTY.

The private PA2 carrier limits each data frame to 4 KiB and each stream to 16
unconfirmed frames. This bounds both data and metadata for one-byte frames. It
separates stdout/stderr, reports EOF and early stdin closure, and validates final
output byte counts before acknowledging delivery. It does not interpret Core's
RPC or framed protocol; only the separate smoke client does so.

## Failure and cleanup

A local resource owner retains the terminal ID and exact random ownership name.
It survives adapter death long enough to attempt cleanup, including a creation
response lost before bootstrap. Reconciliation selects only that exact name;
unrelated terminals are never removed by a prefix search.

After helper startup, an independent remote lease stops abandoned transport work.
Cancellation first sends TERM to the owned foreground group, then forces cleanup
after two seconds even if heartbeats continue. Normal completion also removes
remaining same-group descendants after delivery acknowledgment. Core's persistent
daemon is deliberately outside the transport group and is not killed by the
adapter; its lifecycle remains Core's responsibility.

A partition can prevent creation reconciliation or terminal removal before a
helper exists. Cleanup failure is explicit; the adapter does not claim confirmed
deletion. Keep the private failure evidence and reconcile the exact owned resource
once access returns. Do not use broad process-name or terminal-prefix kills.

The adapter does not replay command bytes or reconnect in place. Core owns a new
transport invocation after connection loss. Exit 255 is the transport-failure
convention; ordinary remote status remains available when delivery and cleanup
complete. Closing a local destination pipe cancels the owned invocation.

## Smoke evidence

Smoke is opt-in network activity. It uses the real artifact and existing poolab
alias. Its Core configuration, state, runtime directory, socket, and session name
are isolated together on both hosts. A session name alone is insufficient.

Reports under ignored `.local/` record the Git revision, dirty/authoritative state,
checks, and cleanup outcomes. Packaged source is directly compared with checkout
source to reject stale artifacts; no separate content-fingerprint scheme is
needed. Previous reports are retained when smoke is rerun. Publish only sanitized
conditions and verdicts, never private paths, process inventories, or server data.

For release acceptance, use a clean committed revision. A passing dirty-tree
smoke is useful development feedback but not final provenance.

## Release

```sh
make check doc-guard
make release DRY_RUN=1
```

Dry-run is offline and performs no GitHub mutation or live smoke. A real release
requires a clean main branch exactly matching origin/main and successful main CI
jobs for both Python versions and documentation guards. It repeats local checks
and actual smoke before publishing.

```sh
make release VERSION=v0.1.0
```

The version must match the package version. Existing tags/releases are never
clobbered. The release script verifies the tag target, asset metadata, and the
downloaded executable against the local artifact by direct comparison. If a
partial remote publication fails, inspect the existing tag/release before any
retry; do not delete or overwrite it automatically.

Merge is a separate prior gate: request `@greptileai review pr`, validate each
finding, and require confidence 5/5 for the current PR revision plus passing CI.
If that service is unavailable or confidence remains lower, leave the PR open
and do not release.

## Rollback and development cleanup

Keep the previous known-good adapter executable before installing a new one. To
roll back, reinstall that executable at the same configured `--via` path. No App
or Core fork, data migration, or server deployment is needed. Existing live
connections must be re-established by Core to use the selected executable.

After merge and verified release, preserve private UAT reports outside the owned
development worktree. Confirm its branch and clean tracked state before removing
that worktree and the corresponding local/remote feature branch. Preserve the
main checkout, operator configuration, unrelated worktrees, and raw historical
evidence.
