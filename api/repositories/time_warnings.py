"""Reviewed source-time conflicts, independent of scraped or scored content."""
from datetime import UTC, datetime
from typing import Any

from api.models.occurrences import provider_identity

TIME_WARNING = 'Date/time unconfirmed. Check with the organizer.'
MAX_EVIDENCE_LENGTH = 2000


def review_time_warning(conn: Any, *, url: str, evidence: str, clear: bool = False) -> int:
    """Flag or resolve one source identity using explicit operator-reviewed evidence."""
    evidence = evidence.strip()
    if not evidence or len(evidence) > MAX_EVIDENCE_LENGTH:
        raise ValueError(f'Evidence must contain 1-{MAX_EVIDENCE_LENGTH} characters')
    identity = provider_identity(url)
    ids = [row['id'] for row in conn.execute('SELECT id, url FROM events').fetchall()
           if provider_identity(row['url']) == identity]
    if not ids:
        raise ValueError('Event source not found')
    placeholders = ','.join('?' for _ in ids)
    conn.execute(f'''UPDATE events SET time_warning=?, time_warning_evidence=?, time_warning_reviewed_at=?
                     WHERE id IN ({placeholders})''',
                 (None if clear else TIME_WARNING, evidence, datetime.now(UTC).isoformat(), *ids))
    return len(ids)
