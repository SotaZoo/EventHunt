"""Atomic invitation requests, approval and recoverable delivery work."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from api.repositories import _storage as storage

SOURCE_LIMIT = 5
SOURCE_WINDOW_SECONDS = 600
DAILY_REQUEST_LIMIT = 100
DAILY_MAIL_LIMIT = 200
DAY_SECONDS = 86400
SEND_UNCERTAINTY_MINUTES = 5


class RequestLimited(Exception):
    """The source or shared pilot abuse budget has been exhausted."""


def row_dict(row):
    """Detach a result from either supported database row representation."""
    return dict(zip(row.keys(), row, strict=True))


class InvitationRepository:
    def __init__(self, *, db_path: Path | None = None, database=None):
        self.db_path, self.database = db_path, database

    @contextmanager
    def transaction(self):
        """Serialize request, approval and mail claims across app processes."""
        if self.database is not None:
            with self.database.compat_connect() as conn:
                conn.execute("UPDATE invitation_guard SET singleton=singleton WHERE singleton=1")
                yield conn
        else:
            conn = storage.connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @staticmethod
    def _limit(conn, key: str, maximum: int, seconds: int) -> None:
        now = int(datetime.now(UTC).timestamp())
        conn.execute("DELETE FROM invitation_limits WHERE expires_at<=?", (now,))
        row = conn.execute("SELECT count FROM invitation_limits WHERE key=?", (key,)).fetchone()
        if row and row["count"] >= maximum:
            raise RequestLimited()
        conn.execute("""INSERT INTO invitation_limits(key,expires_at,count) VALUES (?,?,1)
            ON CONFLICT(key) DO UPDATE SET count=invitation_limits.count+1""", (key, now + seconds))

    @staticmethod
    def _queue(conn, request_id: int, kind: str) -> int:
        conn.execute("""INSERT INTO invitation_mail(request_id,kind,message_id) VALUES (?,?,?)
            ON CONFLICT(request_id,kind) DO NOTHING""", (request_id, kind, str(uuid4())))
        return conn.execute("SELECT id FROM invitation_mail WHERE request_id=? AND kind=?",
                            (request_id, kind)).fetchone()["id"]

    def request(self, email: str, source: str) -> int:
        """Record one normalized request and its notification, within abuse limits."""
        with self.transaction() as conn:
            self._limit(conn, "source:" + source, SOURCE_LIMIT, SOURCE_WINDOW_SECONDS)
            self._limit(conn, "requests", DAILY_REQUEST_LIMIT, DAY_SECONDS)
            conn.execute("""INSERT INTO invitation_requests(email,created_at) VALUES (?,?)
                ON CONFLICT(email) DO NOTHING""", (email, datetime.now(UTC).isoformat()))
            row = conn.execute("SELECT id FROM invitation_requests WHERE email=?", (email,)).fetchone()
            return self._queue(conn, row["id"], "notification")

    def approve(self, request_id: int) -> int:
        """Grant access once, atomically with invitation work; never reinstate."""
        with self.transaction() as conn:
            request = conn.execute("SELECT * FROM invitation_requests WHERE id=?", (request_id,)).fetchone()
            if request is None:
                raise ValueError("Request not found")
            user = conn.execute("SELECT is_active FROM users WHERE email=?", (request["email"],)).fetchone()
            if user and not user["is_active"]:
                raise ValueError("Account is inactive; ordinary approval cannot reinstate it")
            if request["approved_at"]:
                invite = conn.execute("SELECT revoked_at FROM alpha_invites WHERE id=?", (request["invite_id"],)).fetchone()
                if not invite or invite["revoked_at"]:
                    raise ValueError("Access was revoked; ordinary approval cannot reinstate it")
            if not request["approved_at"]:
                old = conn.execute("SELECT revoked_at FROM alpha_invites WHERE normalized_email=? ORDER BY id DESC LIMIT 1",
                                   (request["email"],)).fetchone()
                if old and old["revoked_at"]:
                    raise ValueError("Access was revoked; review it separately before approving")
                invite = storage.create_alpha_invite(conn, email=request["email"])
                conn.execute("UPDATE invitation_requests SET approved_at=?,invite_id=? WHERE id=?",
                             (datetime.now(UTC).isoformat(), invite["id"], request_id))
            return self._queue(conn, request_id, "invitation")

    @staticmethod
    def _recover(conn):
        # A crashed process may have reached the provider. Never automatically
        # resend a stale claim, even if no final receipt was committed.
        cutoff = (datetime.now(UTC) - timedelta(minutes=SEND_UNCERTAINTY_MINUTES)).isoformat()
        conn.execute("""UPDATE invitation_mail SET status='unknown',error_code='interrupted'
            WHERE status='sending' AND attempted_at<?""", (cutoff,))
        conn.execute("""UPDATE invitation_mail SET status='canceled',error_code='access_revoked'
            WHERE kind='invitation' AND status IN ('pending','failed')
            AND request_id IN (
                SELECT r.id FROM invitation_requests r LEFT JOIN alpha_invites a ON a.id=r.invite_id
                LEFT JOIN users u ON u.email=r.email
                WHERE a.id IS NULL OR a.revoked_at IS NOT NULL OR u.is_active=FALSE
            )""")

    def requests(self) -> list[dict]:
        """List operator-only request, access and recoverable delivery state."""
        with self.transaction() as conn:
            self._recover(conn)
            return [row_dict(row) for row in conn.execute("""SELECT r.*,
                a.accepted_at, a.revoked_at, n.status AS notification_status, m.status AS invitation_status,
                n.id AS notification_mail_id, m.id AS invitation_mail_id,
                n.provider_message_id AS notification_provider_id, m.provider_message_id AS invitation_provider_id
                FROM invitation_requests r LEFT JOIN alpha_invites a ON a.id=r.invite_id
                LEFT JOIN invitation_mail n ON n.request_id=r.id AND n.kind='notification'
                LEFT JOIN invitation_mail m ON m.request_id=r.id AND m.kind='invitation' ORDER BY r.id""").fetchall()]

    def pending_mail(self) -> list[int]:
        """Return unsent work that is safe to resume after process restart."""
        with self.transaction() as conn:
            self._recover(conn)
            return [row["id"] for row in conn.execute("SELECT id FROM invitation_mail WHERE status='pending' ORDER BY id").fetchall()]

    def claim(self, mail_id: int, *, retry: bool = False) -> dict | None:
        """Claim one send, preserving uncertainty and current access revocation."""
        with self.transaction() as conn:
            self._recover(conn)
            row = conn.execute("""SELECT m.*, r.email FROM invitation_mail m
                JOIN invitation_requests r ON r.id=m.request_id WHERE m.id=?""", (mail_id,)).fetchone()
            if row is None:
                raise ValueError("Mail not found")
            if row["status"] in {"unknown", "sending"}:
                if retry:
                    raise ValueError("Delivery is uncertain or in progress; reconcile before retrying")
                return None
            if row["status"] not in ({"pending", "failed"} if retry else {"pending"}):
                return None
            self._limit(conn, "mail", DAILY_MAIL_LIMIT, DAY_SECONDS)
            attempt_token = str(uuid4())
            conn.execute("UPDATE invitation_mail SET status='sending',attempted_at=?,attempt_token=?,error_code=NULL WHERE id=?",
                         (datetime.now(UTC).isoformat(), attempt_token, mail_id))
            return {**row_dict(row), "attempt_token": attempt_token}

    def retry(self, mail_id: int) -> str:
        """Queue a known failed send for the hosted worker without resending locally."""
        with self.transaction() as conn:
            self._recover(conn)
            row = conn.execute("SELECT status FROM invitation_mail WHERE id=?", (mail_id,)).fetchone()
            if row is None:
                raise ValueError("Mail not found")
            if row["status"] in {"unknown", "sending"}:
                raise ValueError("Delivery is uncertain or in progress; reconcile before retrying")
            if row["status"] == "failed":
                conn.execute("UPDATE invitation_mail SET status='pending' WHERE id=?", (mail_id,))
                return "pending"
            return row["status"]

    def finish(self, mail_id: int, status: str, error: str | None = None, *, attempt_token: str, provider_message_id: str | None = None) -> None:
        """Record a known send outcome without overwriting operator reconciliation."""
        with self.transaction() as conn:
            conn.execute("UPDATE invitation_mail SET status=?,error_code=?,provider_message_id=? WHERE id=? AND status='sending' AND attempt_token=?",
                         (status, error, provider_message_id, mail_id, attempt_token))

    def mail_status(self, mail_id: int) -> str:
        """Read the current delivery outcome after crash/revocation recovery."""
        with self.transaction() as conn:
            self._recover(conn)
            row = conn.execute("SELECT status FROM invitation_mail WHERE id=?", (mail_id,)).fetchone()
            if row is None:
                raise ValueError("Mail not found")
            return row["status"]

    def reconcile(self, mail_id: int, outcome: str) -> None:
        """Record an operator's provider-backed resolution of an uncertain send."""
        if outcome not in {"sent", "not-sent"}:
            raise ValueError("Outcome must be sent or not-sent, based on provider evidence")
        with self.transaction() as conn:
            self._recover(conn)
            result = conn.execute("""UPDATE invitation_mail SET status=?,error_code='operator_reconciled'
                WHERE id=? AND status='unknown'""", ("sent" if outcome == "sent" else "failed", mail_id))
            if not result.rowcount:
                raise ValueError("Only uncertain deliveries can be reconciled")
