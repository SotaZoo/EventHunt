ALTER TABLE events ADD COLUMN registration_availability TEXT NOT NULL DEFAULT 'unknown'
    CHECK (registration_availability IN ('unknown', 'open', 'waitlist', 'full', 'closed', 'canceled'));
ALTER TABLE events ADD COLUMN availability_evidence TEXT;
ALTER TABLE events ADD COLUMN availability_checked_at TEXT;
ALTER TABLE events ADD COLUMN availability_attempted_at TEXT;
