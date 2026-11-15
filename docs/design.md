# Standalone adapter design

The implementation contract is [specification #1](https://github.com/proletariat64/poolab-adapter/issues/1).
This document distinguishes verified carrier experiments from production work.

## Boundary

Herdr App and Core remain unchanged. Local Core starts the adapter with one
complete remote shell command as its final argument. The adapter treats Core
stdin/stdout as opaque bytes and keeps stderr separate.

The poolab CLI remains the authentication and transport authority. The adapter
uses fresh exclusively owned terminals, never SSH, undocumented credential
extraction, or a new listener. The actual remote command receives pipes rather
than a PTY. Private carrier PTYs do not become the remote command's stdio.

## Verified prior art

See the [sanitized experiment summary](experiments.md). The experiments showed
that a local PTY is needed to enable poolab attach input. They also exposed fast
output closure, detached-child leaks, stdin-closure races, and inherited-pipe
cancellation descendants.

Receiver-driven flow limits, serialized frames, independent liveness handling,
explicit stream lifecycle messages, and owned-group termination address those
cases at the adapter boundary. poolab's own successful exit is not sufficient
evidence of remote success.

The experimental programs are not production entry points: their known-output
buffers, fixed startup delay, and private runtime reports belong to testing.

## Production components

- A CLI exposes the `--via` process boundary and preserves the remote command.
- A resource owner creates the named terminal, owns attach, and attempts exact
  cleanup even when the CLI dies during startup.
- A bounded relay separates carrier reads/control from local destination pipes.
- A bundled remote helper gives the command ordinary pipes, reports stream
  completion and delivered counts, and enforces remote liveness and cancellation.
- A standalone packaged executable contains these components without third-party
  runtime dependencies.

The private carrier protocol may evolve together on both ends. The production
version limits both decoded payload and frame counts; tiny data frames must not
turn a byte allowance into an unbounded metadata or credit-message burst.

## Completion and failure

A successful command requires both output-stream EOF records, a complete final
status record, matching delivered output counts, and drained local destinations.
The receiver acknowledges final delivery before helper termination. EOF without
that record is transport failure. Ordinary remote status, including 37 or 127,
must remain distinguishable from a transport error.

Cancellation must stop same-group descendants even when they inherit pipes or
ignore graceful signals. A separate force deadline must not be extended by
heartbeats. Persistent Core daemons that deliberately leave the transport's
owned group remain Core's responsibility.

Remote leases apply once the helper starts. Creation/bootstrap races require
local ownership and exact-name reconciliation; a partition preventing cleanup
must be reported as an operational limitation, not hidden as confirmed deletion.

## Verification

The primary seam is the real adapter executable: stdio, exit status, signals,
and owned resources. Offline integration uses a local poolab test double while
retaining actual pipes, PTYs, and processes. Tests must not depend on private
relay method structure.

Real smoke uses existing poolab access and an isolated Core session. Configuration,
state, runtime paths, and socket are isolated together; a session name alone is
insufficient. UAT covers hello, terminal traffic, transport disconnect, reconnect,
and exact-resource cleanup. It does not touch the existing App or default Core.

GitHub Actions runs offline regression and documentation guards. The PR requests
Greptile review; merge requires passing checks and confidence 5/5 attributable to
the current revision. Release follows merged-revision smoke, and only successful
release permits cleanup of the owned development worktree and branch.

## Public references

- [Core custom transports](https://github.com/herdrdev/herdr-core/blob/c84d6e76156a6c2aac1da20efbe800f10acfe897/docs/machines.md)
- [Core remote command construction](https://github.com/herdrdev/herdr-core/blob/c84d6e76156a6c2aac1da20efbe800f10acfe897/crates/herdr-core/src/machines.rs)
- [Core endpoint and state paths](https://github.com/herdrdev/herdr-core/blob/c84d6e76156a6c2aac1da20efbe800f10acfe897/crates/herdr-core/src/paths.rs)
