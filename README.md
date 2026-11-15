# poolab-adapter

`herdr-poolab` is a standalone poolab transport for Herdr Core. It preserves the
remote shell command and binary stdio without SSH or changes to Herdr App/Core.

Runtime requirements: Linux/WSL, Python 3.11+, an existing authenticated poolab
installation, and Python 3.11+ on the remote target. The remote Core must already
be installed. The adapter does not install or upgrade Core or copy credentials.

## Build and use

```sh
make package
install -d "$HOME/.local/bin"
install -m 755 dist/herdr-poolab "$HOME/.local/bin/herdr-poolab"
herdr-poolab --version
```

A release contains the same executable Python zipapp, including its MIT license.
Once a release is published, it can also be downloaded with `gh release download`
using the asset name `herdr-poolab` from this repository.

Use the absolute installed path in Core's custom transport command:

```sh
hc machine add --name remote \
  --via "'$HOME/.local/bin/herdr-poolab' --alias default --"
```

This registration is an operator action, not part of installation or smoke. Use
an existing poolab alias instead of `default` when needed. The final `--` keeps
Core's complete remote shell line as one argument.

```text
Herdr App -> local Herdr Core -> herdr-poolab -> poolab -> remote Herdr Core
```

Stdout contains only remote stdout; stderr remains separate. Input EOF closes
only remote stdin. Slow consumers apply bounded backpressure. Early remote stdin
closure stops local input while output and status continue. Remote exit codes
are preserved; signals map to `128 + signal`. Transport failure returns 255 with
an adapter diagnostic, like SSH's transport-error convention. A remote command
can itself return 255, so the diagnostic distinguishes that case.

## Development and verification

```sh
make bootstrap               # create/reuse the owned feature worktree and venv
# Continue in the worktree printed by bootstrap.
make check                   # offline syntax, Ruff, and process/PTY regressions
make doc-guard               # document placement, encoding, links, public-data guard
make package                 # dependency-free executable in dist/
make smoke                   # real poolab and isolated local/remote Core acceptance
make release DRY_RUN=1        # offline packaging/version check, no publication
```

Bootstrap supports `BRANCH` and `WORKTREE` overrides. It preserves existing work
and refuses to overwrite unrelated directories. Only development tooling needs
Ruff; the executable has no third-party Python runtime dependencies.

GitHub Actions runs offline `check` on Python 3.11/3.12 and a separate `doc-guard`
job. It does not receive private-network credentials or run live smoke.

## Real-environment UAT

`POOLAB_ALIAS` selects the existing target; its default is `default`.

```sh
POOLAB_ALIAS=default make smoke
```

Smoke uses the packaged executable, verifies binary streams and status, then
creates uniquely isolated Core configuration, state, runtime, socket, and session
names. It tests hello, framed terminal attach/input, reconnect, and local Core
`--via` registration through an owned isolation wrapper. It does not control the
existing App or default Core session. Owned terminals and Core instances are
removed afterward; uncertain cleanup makes smoke fail and preserves evidence.

Evidence stays private under ignored `.local/`; reruns preserve previous reports.
Raw server responses and operational captures are not published. See the
[operator guide](docs/operations.md) for lifecycle limits and release procedure.

## Delivery policy

[Specification #1](https://github.com/proletariat64/poolab-adapter/issues/1)
requires passing CI and real smoke before PR acceptance, followed by Greptile
confidence 5/5 on the current revision. Review unavailability blocks merge rather
than waiving that condition. Release runs from clean merged main, verifies its CI,
reruns smoke, publishes a non-overwriting versioned asset, and verifies download
contents. Development resources are removed only after successful publication.

## Design and provenance

- [Transport design](docs/design.md)
- [Sanitized experimental prior art](docs/experiments.md)
- [Project vocabulary](GLOSSARY.md)
- [Agent instructions](AGENTS.md)

## License

[MIT](LICENSE).
