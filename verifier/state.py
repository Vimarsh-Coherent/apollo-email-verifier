"""Durable, resumable per-candidate state in SQLite.

A run can be killed and restarted; anything already resolved is skipped, and
tempfails scheduled for a layer-6 retry survive the restart. WAL mode plus a
short busy timeout lets the worker threads share one file safely.
"""

import json
import sqlite3
import threading
import time

STATUS_PENDING = "pending"
STATUS_RETRY = "retry"        # tempfail waiting for its backoff window
STATUS_DONE = "done"
STATUS_ERROR = "error"


class StateStore:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._init_schema()

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_schema(self):
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_email TEXT PRIMARY KEY,
                row_id          INTEGER,
                person_id       TEXT,
                name            TEXT,
                domain          TEXT,
                pattern         TEXT,
                rank            INTEGER,
                is_known        INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'pending',
                attempts        INTEGER DEFAULT 0,
                next_attempt_at REAL DEFAULT 0,
                verdict         TEXT,
                confidence      INTEGER,
                rcpt_code       INTEGER,
                catch_all       INTEGER,
                reasons         TEXT,
                layers_json     TEXT,
                last_ip         TEXT,
                updated_at      REAL
            );
            CREATE INDEX IF NOT EXISTS idx_status ON candidates(status);
            CREATE INDEX IF NOT EXISTS idx_domain ON candidates(domain);
            CREATE INDEX IF NOT EXISTS idx_person ON candidates(row_id);
            """
        )

    def seed(self, candidates):
        """Insert candidates that are not already tracked. Returns count added."""
        conn = self._conn()
        added = 0
        with self._write_lock:
            conn.execute("BEGIN")
            for c in candidates:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO candidates
                       (candidate_email, row_id, person_id, name, domain, pattern,
                        rank, is_known, status, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        c["candidate_email"], c.get("row_id"), c.get("id"),
                        c.get("name"), c.get("domain"), c.get("pattern"),
                        c.get("rank"), 1 if c.get("is_known_email") else 0,
                        STATUS_PENDING, time.time(),
                    ),
                )
                added += cur.rowcount
            conn.execute("COMMIT")
        return added

    def reset_stale_claims(self):
        """Return orphaned 'claimed' rows to 'pending'.

        A row is left 'claimed' if the process died mid-probe. Without this,
        claim_batch never re-selects them yet has_open_work still counts them,
        so a resumed run would spin forever. Call once at startup.
        """
        conn = self._conn()
        with self._write_lock:
            cur = conn.execute(
                "UPDATE candidates SET status=? WHERE status='claimed'",
                (STATUS_PENDING,),
            )
        return cur.rowcount

    def claim_batch(self, limit, now=None):
        """Atomically claim work: pending items and retries whose time has come."""
        now = now if now is not None else time.time()
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT * FROM candidates
                   WHERE status = ? OR (status = ? AND next_attempt_at <= ?)
                   ORDER BY rank ASC
                   LIMIT ?""",
                (STATUS_PENDING, STATUS_RETRY, now, limit),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE candidates SET status='claimed', updated_at=? WHERE candidate_email=?",
                    (now, row["candidate_email"]),
                )
            conn.execute("COMMIT")
        return [dict(r) for r in rows]

    def mark_retry(self, email, attempts, next_attempt_at, last_ip):
        self._update(email, status=STATUS_RETRY, attempts=attempts,
                     next_attempt_at=next_attempt_at, last_ip=last_ip)

    def mark_done(self, email, verdict, confidence, rcpt_code, catch_all,
                  reasons, layers, attempts, last_ip):
        self._update(
            email, status=STATUS_DONE, verdict=verdict, confidence=confidence,
            rcpt_code=rcpt_code,
            catch_all=None if catch_all is None else (1 if catch_all else 0),
            reasons=json.dumps(reasons), layers_json=json.dumps(layers),
            attempts=attempts, last_ip=last_ip,
        )

    def mark_error(self, email, reasons, attempts, last_ip):
        self._update(email, status=STATUS_ERROR, verdict="unknown",
                     reasons=json.dumps(reasons), attempts=attempts, last_ip=last_ip)

    def _update(self, email, **fields):
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [email]
        conn = self._conn()
        with self._write_lock:
            conn.execute(f"UPDATE candidates SET {cols} WHERE candidate_email=?", values)

    def person_has_hit(self, row_id):
        """True if any candidate for this person is already deliverable."""
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM candidates WHERE row_id=? AND verdict='deliverable' LIMIT 1",
            (row_id,),
        ).fetchone()
        return row is not None

    def counts(self):
        conn = self._conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM candidates GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        vrows = conn.execute(
            "SELECT verdict, COUNT(*) n FROM candidates WHERE verdict IS NOT NULL GROUP BY verdict"
        ).fetchall()
        by_verdict = {r["verdict"]: r["n"] for r in vrows}
        return {"status": by_status, "verdict": by_verdict}

    def has_open_work(self, now=None):
        now = now if now is not None else time.time()
        conn = self._conn()
        row = conn.execute(
            """SELECT 1 FROM candidates
               WHERE status IN (?, 'claimed') OR (status=? AND next_attempt_at > ?)
               LIMIT 1""",
            (STATUS_PENDING, STATUS_RETRY, now),
        ).fetchone()
        return row is not None

    def export_rows(self):
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM candidates ORDER BY row_id, rank"
        ).fetchall()
        return [dict(r) for r in rows]
