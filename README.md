# poolab-adapter

A standalone poolab transport adapter for Herdr Core, without SSH or changes to
Herdr App/Core. Runtime support targets Linux/WSL with Python 3.11+ and an existing
authenticated poolab installation.

## Status

The transport experiments are verified; the production `herdr-poolab` executable
is being implemented under [specification #1](https://github.com/proletariat64/poolab-adapter/issues/1).
An experiment is not a supported `--via` entry point.

```text
Herdr App -> local Herdr Core -> herdr-poolab -> poolab -> remote Herdr Core
```

Core gives the adapter a complete remote shell command as its final argument.
The adapter must preserve binary stdin/stdout, separate stderr, handle half-close
and backpressure, and clean only resources it owns. Existing poolab authentication
is reused; no credential copying or new public listener is needed.

## Development

Use the repository's Make targets. Bootstrap creates or reuses an owned feature
worktree and its development environment; it preserves the original checkout.

```sh
make bootstrap
# Continue in the worktree printed by bootstrap.
make check
make doc-guard
```

`make check` is offline. It runs Python syntax, Ruff, and regression tests.
`make doc-guard` checks document placement, encoding, links, and public-content
boundaries. GitHub Actions will run both before the implementation PR is ready.

The production smoke and release targets are part of specification #1. Live
acceptance will use a configured poolab alias and a uniquely isolated Core
session, never the operator's existing App or default Core session.

## Evidence and design

- [Transport design](docs/design.md)
- [Sanitized experiment summary](docs/experiments.md)
- [Project vocabulary](GLOSSARY.md)
- [Agent instructions](AGENTS.md)

Raw experiment captures, local account paths, process inventories, and operational
logs stay outside public version control. New smoke evidence must follow the same
boundary; public reports contain only sanitized results.

## License

[MIT](LICENSE).
