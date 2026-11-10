ALTER TABLE events ADD COLUMN registration_availability TEXT NOT NULL DEFAULT 'unknown'
    CHECK (registration_availability IN ('unknown', 'open', 'waitlist', 'full', 'closed', 'canceled'));
ALTER TABLE events ADD COLUMN availability_evidence TEXT;
ALTER TABLE events ADD COLUMN availability_checked_at TIMESTAMPTZ;
ALTER TABLE events ADD COLUMN availability_attempted_at TIMESTAMPTZ;

-- The restricted worker already has event-table INSERT/UPDATE privileges.
-- No auth-data permissions or new role grants are needed.
