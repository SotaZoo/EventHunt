# Deployment

The [Dockerfile](../../Dockerfile) builds the web assets and installs the
Python package with its migration and deployment files. At startup it
applies pending migrations, then runs `nyc-ai-scout serve`.

Configure the [database](database.md) and [authentication](authentication.md)
for your deployment. A shared deployment needs its own HTTPS base URL and
session secret. Use your host's environment configuration for credentials.

Run collection separately from the web process. The [operations
guide](operations.md) explains source selection, the ranking profile, and
the worker cycle. Verify the tested source revision, then check `/healthz`
and the relevant web and MCP flows.
