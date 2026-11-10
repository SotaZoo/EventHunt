ALTER TABLE events ADD COLUMN registration_url TEXT;

-- Existing event-table grants cover this source fact; no auth access changes.
