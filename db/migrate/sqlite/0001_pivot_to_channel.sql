-- db/migrate/sqlite/0001_pivot_to_channel.sql
-- Pivot from M0 classifier schema to channel-only v1.
-- Rename starts_at -> start_at; drop dead classifier columns; add fit/scoring/posted.

BEGIN;

-- Drop old indexes first (SQLite blocks dropping columns that have indexes).
DROP INDEX IF EXISTS idx_events_starts;
DROP INDEX IF EXISTS idx_events_classify;
DROP INDEX IF EXISTS idx_events_relevance;

ALTER TABLE events RENAME COLUMN starts_at TO start_at;

-- M0 classifier path is dead; we score directly via score_batch.
ALTER TABLE events DROP COLUMN ai_relevance;
ALTER TABLE events DROP COLUMN classify_status;
ALTER TABLE events DROP COLUMN tags_json;
ALTER TABLE events DROP COLUMN is_nyc;
ALTER TABLE events DROP COLUMN category;
ALTER TABLE events DROP COLUMN summary;
ALTER TABLE events DROP COLUMN cover_image;

ALTER TABLE events ADD COLUMN fit_input_hash         TEXT;
ALTER TABLE events ADD COLUMN fit_score              REAL CHECK (fit_score IS NULL OR (fit_score BETWEEN 0.0 AND 1.0));
ALTER TABLE events ADD COLUMN fit_reasoning          TEXT;
ALTER TABLE events ADD COLUMN urgency                TEXT CHECK (urgency IS NULL OR urgency IN ('low','medium','high'));
ALTER TABLE events ADD COLUMN urgency_reason         TEXT;
ALTER TABLE events ADD COLUMN scoring_status         TEXT NOT NULL DEFAULT 'pending'
                              CHECK (scoring_status IN ('pending','scored','failed','skipped'));
ALTER TABLE events ADD COLUMN scoring_failed_reason  TEXT;
ALTER TABLE events ADD COLUMN prompt_version         TEXT;
ALTER TABLE events ADD COLUMN taste_sha              TEXT;
ALTER TABLE events ADD COLUMN posted_at              TEXT;

-- Backfill: pre-pivot rows are treated as "already considered" so the channel doesn't flood.
UPDATE events SET posted_at = datetime('now') WHERE posted_at IS NULL;

-- Create v1 indexes.
CREATE INDEX IF NOT EXISTS idx_events_posted_at      ON events(posted_at);
CREATE INDEX IF NOT EXISTS idx_events_scoring_status ON events(scoring_status);
CREATE INDEX IF NOT EXISTS idx_events_start_at       ON events(start_at);
CREATE INDEX IF NOT EXISTS idx_events_fit_score      ON events(fit_score);

COMMIT;
