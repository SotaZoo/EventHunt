# Runtime

- The Starlette app serves the web interface and optional MCP endpoint.
- The collection worker and web process can share a PostgreSQL catalog.
  SQLite is the default for local development.
- Use `nyc-ai-scout cycle` to collect events, refresh availability, and score
  eligible records. Use `nyc-ai-scout serve` to read the catalog on the web.
- Build discovery assets before installing the Python package.
- See [Operations](../doc/administration/operations.md) for feed and scoring
  configuration and [Deployment](../doc/administration/deployment.md) for
  hosting requirements.
