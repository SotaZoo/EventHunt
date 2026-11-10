ALTER TABLE events ADD COLUMN attendance_mode TEXT
    CHECK (attendance_mode IS NULL OR attendance_mode IN ('in-person', 'online', 'mixed'));
