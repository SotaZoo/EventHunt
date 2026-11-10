ALTER TABLE events ADD COLUMN highlight TEXT;
ALTER TABLE events ADD COLUMN urgency_note TEXT;
ALTER TABLE events ADD COLUMN scored_at TIMESTAMPTZ;
ALTER TABLE events ADD COLUMN source_content_checked_at TIMESTAMPTZ;

-- Existing event-table worker grants already cover these nullable columns.
-- Old packages can keep using their insert/update shape; no auth grants change.
