# Backend (api/)

## Entry points

- HTTP app: `api.app:create_app` (`uv run nyc-ai-scout serve`)
- CLI: `api.commands.main:app` (`uv run nyc-ai-scout --help`)
- Agent discovery: authenticated remote MCP at `/mcp`; see
  [Agent event discovery](mcp.md)

## Layers

- `api/controllers/`: HTTP/JSON handlers
- `api/services/`: Domain services (authorization, sessions, event_catalog)
- `api/repositories/`: DB access split per aggregate
- `api/integrations/`: External services (`google/`, `discord/`, `llm/`, `scrapers/`)
- `api/tasks/`: Background jobs
- `api/schedule/`: launchd helpers
- `api/configs/`: Settings + env reads
- `api/models/`: Pydantic domain models

## Adding a new endpoint

1. Add a controller in `api/controllers/`.
2. Add a service if the logic isn't trivial.
3. Wire the route into `api.app.create_app` (or extend the legacy registration).
4. Add an integration test in `api/tests/integration_tests/`.

## CLI commands

The CLI is registered in `api.commands.main`, with focused command modules
under `api/commands/`. Add commands as Typer functions; cover them in
`api/tests/{unit_tests,integration_tests}/`.
