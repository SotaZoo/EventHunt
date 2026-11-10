# LLM & Prompt Behavior

## Prompts

- Current prompt: `api/integrations/llm/prompts/scoring/v5.md`. It uses the
  supplied profile for location preferences and preserves the scoring and
  annotation schema. Older versions remain available for backtests.
- Versioned by filename. Bump version on any wording change.

## Modes

- `LLM_SCORING_MODE=mock`: read fixtures from `api/tests/fixtures/scoring.json`
- `LLM_SCORING_MODE=codex-cli` (default): subprocess fallback to local `codex exec`
- `CODEX_MODEL=gpt-5.6-luna`: cost-sensitive current Codex CLI scoring model
- `CODEX_REASONING_EFFORT=low`: scoring should not inherit interactive xhigh reasoning
- The scoring subprocess uses `--ignore-user-config` so it does not load the
  interactive account's MCP/plugin configuration; account authentication is
  still required.
- `LLM_SCORING_MODE=pi-auth`: use Anthropic through pi-auth OAuth bearer
- `LLM_SCORING_MODE=cli`: subprocess fallback to local `claude -p`
- `LLM_SCORING_MODE=api`: direct Anthropic API key

## Scoring shape

- `EventFit` retains event ID, numeric fit, full reasoning, legacy urgency,
  notable people, event type and food, with optional `highlight` and `urgency_note`.
- All fields validated by Pydantic at the boundary.
- The v2 extraction fields (`notable_people`, `event_type`, `food_provided`)
  default to empty / `None` so persisted v1 rows keep round-tripping through
  the schema unchanged.
- Annotation text is bounded to 200 characters and requires a captured
  source-evidence span (up to 400 characters). Malformed optional annotations
  are dropped without discarding the core score. Exact evidence matching is
  a structural check, not proof of semantic accuracy; real-model evaluation
  must also check names, roles, conditions, generic claims and injections.
- Highlight is a concrete positive reason, never full reasoning or urgency.
  The model selects a verbatim `deadline_evidence` expression within the
  supporting span; the application normalizes it and formats its text. Complete
  year/date/time/timezone context is required. Scrape time, today and event start
  never supply missing statement context; unresolved relative times remain unknown.
  No collector currently supplies reliable statement-bound reference timestamps.
  Deadline notes need an aware future instant; scarcity is qualified as an
  observation. Web and MCP share qualification, including a one-day source
  content observation cutoff. Partial preserved descriptions do not renew it.
- The current prompt refreshes eligible older scores within the existing per-cycle budget,
  behind pending rows. Each row gets one batch attempt per cycle; the scorer's
  own retry remains. Refresh failure preserves a valid old score.

## Stamping

Every persisted score includes `prompt_version`, `taste_sha` and `scored_at` so a
later precision/recall sweep can attribute drift to prompt or taste changes.
Persistence is guarded by the scoring-input fingerprint so a late model result
cannot overwrite changed source inputs.
Captured legacy NULL fingerprints are guarded too; an omitted guard is a
separate legacy-call state, never inferred from a NULL snapshot.
