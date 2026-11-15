# Repository agent instructions

Build a standalone poolab transport adapter for Herdr Core. Treat Core payloads
as opaque bytes; keep Herdr App and Core unchanged.

This is an independent Python repository. Use its checked-in Make targets rather
than the parent website's build or deployment workflows. Keep development in the
owned feature worktree and leave private runtime captures ignored by Git.

For transport design and verification, read docs/design.md. Distinguish
experimental evidence from production acceptance.

Before publishing this public repository, exclude credentials, internal
endpoints, and private environment artifacts.

## Agent skills

### Issue tracker

Track issues and specifications in GitHub Issues. See docs/agents/issue-tracker.md.

### Triage labels

Use the five default triage labels. See docs/agents/triage-labels.md.

### Domain docs

Use a single-context layout. See docs/agents/domain.md.
