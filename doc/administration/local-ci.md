# Verifying a source checkout

From a clean checkout, run the repository verifier with Python 3.11, uv,
and Node.js available:

```sh
uv run --no-project --python 3.11 python scripts/verify.py
```

It builds the web assets and runs documentation, lint, backend, frontend,
and browser checks against synthetic data. The `--source-sha` and `--receipt`
options record the exact source revision and the checks that ran. See
[Testing](../development/testing.md) for individual checks.
