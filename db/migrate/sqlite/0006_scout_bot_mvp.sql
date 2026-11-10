-- Scout-bot MVP: schema additions for the Discord-bot push trigger,
-- capacity ticker, LLM extraction extension (notable_people, event_type,
-- food_provided), and operational telemetry.
--
-- All changes are additive (no NOT NULL on existing tables, no FK on
-- new columns). Safe to apply with live data; no backfill required.
--
-- Postgres parity: deferred until cloud-deployment migration completes.

BEGIN;

----------------------------------------------------------------------
-- 1. Discord identity mapping per friend.
--
-- NULL means the user has not yet linked Discord via `/scout link`.
-- The unique index excludes NULLs so multiple users can be unlinked,
-- but each linked Discord id is enforced as one-to-one.
----------------------------------------------------------------------

ALTER TABLE users ADD COLUMN discord_user_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_discord_user_id
  ON users(discord_user_id)
  WHERE discord_user_id IS NOT NULL;

----------------------------------------------------------------------
-- 2. Push state on events.
--
-- discord_push_message_id: captured from Discord's POST response when
--   `?wait=true` is set. Required for capacity ticker PATCH and for
--   /scout retract edits. NULL = never pushed.
--
-- last_known_capacity: spot count from the most recent capacity poll.
--   Used by the capacity_poller task to detect threshold crossings.
--
-- cap_{50,25,10}_fired: idempotency flags for the capacity ticker.
--   Set to 1 only after the corresponding PATCH succeeds (PATCH-first,
--   then write). Each threshold fires at most once per event.
--
-- cancelled_at: set to ISO timestamp when Luma reports the event as
--   cancelled. Prevents duplicate cancellation PATCHes.
----------------------------------------------------------------------

ALTER TABLE events ADD COLUMN discord_push_message_id TEXT;
ALTER TABLE events ADD COLUMN last_known_capacity INTEGER;
ALTER TABLE events ADD COLUMN cap_50_fired INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN cap_25_fired INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN cap_10_fired INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN cancelled_at TEXT;

----------------------------------------------------------------------
-- 3. LLM extraction extension (per spec §5).
--
-- event_type: 6-bucket taxonomy
--   talk | workshop | hackathon | demo | networking | other
--   The render layer in format_embed uses this for the `type:` row.
--
-- notable_people: JSON list of {name, role_clause, confidence}.
--   Render layer filters out entries with confidence < 0.85.
--   Empty list = format omits the `notable people:` line.
--
-- food_provided: yes | no | unknown.
--   Three-state at the schema layer to preserve information; the render
--   layer collapses `no` -> `unknown` (only `yes` carries push-signal).
----------------------------------------------------------------------

ALTER TABLE events ADD COLUMN event_type TEXT;
ALTER TABLE events ADD COLUMN notable_people TEXT;
ALTER TABLE events ADD COLUMN food_provided TEXT;

----------------------------------------------------------------------
-- 4. Index for the hourly scan-and-push query.
--
-- The scan query is:
--   SELECT id FROM events
--    WHERE discord_push_message_id IS NULL
--      AND start_at > datetime('now')
--    ORDER BY fit_score DESC
--    LIMIT 1
--
-- The partial index covers the filter (discord_push_message_id IS NULL)
-- and provides fit_score-ordered traversal so the LIMIT 1 is fast even
-- as the events table grows.
----------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_events_unpushed_future_by_score
  ON events(fit_score DESC, start_at)
  WHERE discord_push_message_id IS NULL;

----------------------------------------------------------------------
-- 5. Operational telemetry (per spec §11).
--
-- Append-only time-series table. One row per scout-bot event. Keeps
-- cycle_health (1-row state) clean while preserving per-tick / per-push
-- data needed for threshold tuning, drift detection, and feature-health
-- monitoring.
--
-- event_kind values:
--   tick_scan          per cycle tick: top1_score, qualified_count
--   push_attempt       per push: response_code, message_id_captured
--   capacity_poll      per poll: capacity, threshold_crossings
--   capacity_patch     per patch: threshold, status (success|fail)
--   retract            per retract: invoker
--   slash_command      per command: command, success
--   friend_signal      per push render: rendered_count, status_set used
----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scout_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'tick_scan',
        'push_attempt',
        'capacity_poll',
        'capacity_patch',
        'retract',
        'slash_command',
        'friend_signal'
    )),
    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    payload TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scout_metrics_kind_time
  ON scout_metrics(event_kind, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_scout_metrics_event_id
  ON scout_metrics(event_id)
  WHERE event_id IS NOT NULL;

COMMIT;
