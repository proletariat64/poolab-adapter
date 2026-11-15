# Sanitized carrier experiment summary

These results establish prior art, not acceptance of the standalone executable.
The actual adapter and isolated Core UAT must pass their own gates.

## Environment

Live experiments used an operator-configured authenticated poolab target, poolab
0.8.2, remote Python 3.12, and local/remote Herdr Core 0.1.21. No SSH, credential
copy, new listener, Core installation, or machine registration was needed.

Private hostnames, account paths, process IDs, terminal IDs, raw output, and
operational captures are intentionally excluded from this public summary.

## Observations

- Piped poolab attach stdin was read-only; an exclusively owned local PTY enabled
  input while attach stdout remained a pipe.
- Startup output contained prompts, echoes, and shell integration. None belongs
  in adapter protocol stdout.
- Raw-mode samples preserved every byte value, including invalid UTF-8. ASCII
  carrier framing exists for multiplexed data/lifecycle control, not because
  those samples proved raw PTYs corrupt bytes.
- Large unrestricted output repeatedly closed attach before completion while
  the remote helper and command remained alive. Input-only transfers passed.
  Increasing the exit-acknowledgment wait did not prevent the failure; pacing the
  same output did. The exact poolab-internal closure reason was not established.
- Receiver-driven limits passed twenty consecutive transfers of 8 MiB in each
  direction, with a five-second reader pause, separate stderr, and remote exit 37.
- Actual local-owner SIGTERM/SIGKILL checks passed. Follow-up regression cases
  covered one-byte frames, early stdin closure, blocked pipes, and cancellation
  descendants. Sixteen offline tests passed at the experiment stage.
- A post-review large-transfer run, owner-death cases, and a live early-stdin-close
  case passed. The early-close case deliberately stopped input while preserving
  stdout and status; it was not counted as a complete input transfer.

## Ownership lessons

Detach is not cleanup. Terminal removal did not stop children that had detached
from the terminal's session. The helper must keep the command in its owned group,
use an independent liveness lease, and bound cancellation even when heartbeats
continue. A local resource owner is also needed before helper startup.

Raw evidence remains local and ignored by Git. Public verification reports must
contain only conditions, byte counts, verdicts, and non-sensitive limitations.
