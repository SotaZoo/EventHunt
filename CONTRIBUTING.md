# Contributing

From the repository root:

```sh
npm --prefix web ci --ignore-scripts --no-audit --no-fund
npm --prefix web run build:discovery
uv sync --extra dev --locked --python 3.11
```

Start with the [local demo](README.md#quick-start). See the
[architecture guide](doc/development/architecture.md) for code layout and
[testing](doc/development/testing.md) for fixtures and browser checks.

## Changes

Create a focused branch, preserve the existing application paths, and
update the relevant guide when user-facing commands or configuration change.
Include a reproduction or example and the checks you ran in the pull request.

## Checks

```sh
uv run --locked python scripts/verify.py
```

The verifier installs locked dependencies, builds the web assets, and runs
documentation, lint, backend, frontend, and browser checks. Focused checks:

```sh
uv run --locked pytest api/tests
npm --prefix web test -- --runInBand
uv run --locked python scripts/verify.py --check docs
```

See [Security](SECURITY.md) for private reports.
