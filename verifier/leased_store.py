"""SQLite work queue with domain-level leasing and failover.

This is the store the COORDINATOR process runs. It extends the plain StateStore
with two things needed to share one queue across several VPSs safely:

  1. Domain leasing - a whole domain is handed to exactly one node at a time, so
     that node's in-memory rate limiter owns the domain's pacing. Two nodes never
     probe the same domain at once (which would trip the per-domain caps and get
     the IPs blocked).

  2. Failover - each lease carries an expiry. A live node heartbeats to push its
     leases forward; if a node dies, its leases lapse and reap_expired() frees the
     domain's stranded in-flight rows so another node reclaims them.

Because only the single coordinator process touches this database, correctness
comes from one in-process lock - no distributed locking needed. SQLite is just
durability. Timestamps are epoch seconds (float), matching StateStore.
"""

import time

from .state import StateStore, STATUS_PENDING, STATUS_RETRY

STATUS_CLAIMED = "claimed"


class LeasedStore(StateStore):
    def __init__(self, path, lease_seconds=300.0):
        super().__init__(path)
        self.lease_seconds = float(lease_seconds)
        self._extend_schema()

    def _extend_schema(self):
        conn = self._conn()
        with self._write_lock:
            existing = {r[1] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()}
            if "claimed_by" not in existing:
                conn.execute("ALTER TABLE candidates ADD COLUMN claimed_by TEXT")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS domain_leases (
                    domain           TEXT PRIMARY KEY,
                    leased_by        TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lease_expiry
                    ON domain_leases(lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_domain_status
                    ON candidates(domain, status);
                """
            )

    # ------------------------------------------------------------------
    # Claiming: lease one domain to a node, then claim that domain's due rows.
    # ------------------------------------------------------------------

    def claim_for_node(self, node_id, limit, now=None):
        now = now if now is not None else time.time()
        expires = now + self.lease_seconds
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")

            # 1. A domain that has due work and is not held by another live node
            #    (unleased, expired, or already ours all qualify).
            row = conn.execute(
                """
                SELECT c.domain FROM candidates c
                WHERE (c.status=? OR (c.status=? AND c.next_attempt_at <= ?))
                  AND NOT EXISTS (
                      SELECT 1 FROM domain_leases dl
                      WHERE dl.domain=c.domain
                        AND dl.leased_by <> ?
                        AND dl.lease_expires_at > ?
                  )
                ORDER BY c.rank ASC
                LIMIT 1
                """,
                (STATUS_PENDING, STATUS_RETRY, now, node_id, now),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return []
            domain = row["domain"]

            # 2. Take (or renew) the lease for this node.
            conn.execute(
                """
                INSERT INTO domain_leases (domain, leased_by, lease_expires_at)
                VALUES (?,?,?)
                ON CONFLICT(domain) DO UPDATE SET
                    leased_by=excluded.leased_by,
                    lease_expires_at=excluded.lease_expires_at
                """,
                (domain, node_id, expires),
            )

            # 3. If we just took over an expired lease, free any rows the dead
            #    owner left stuck in 'claimed' so we can pick them up now.
            conn.execute(
                "UPDATE candidates SET status=?, claimed_by=NULL "
                "WHERE domain=? AND status=? AND (claimed_by IS NULL OR claimed_by <> ?)",
                (STATUS_PENDING, domain, STATUS_CLAIMED, node_id),
            )

            # 4. Claim this domain's due rows.
            rows = conn.execute(
                """
                SELECT * FROM candidates
                WHERE domain=? AND (status=? OR (status=? AND next_attempt_at <= ?))
                ORDER BY rank ASC
                LIMIT ?
                """,
                (domain, STATUS_PENDING, STATUS_RETRY, now, limit),
            ).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE candidates SET status=?, claimed_by=?, updated_at=? "
                    "WHERE candidate_email=?",
                    (STATUS_CLAIMED, node_id, now, r["candidate_email"]),
                )
            conn.execute("COMMIT")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Failover machinery.
    # ------------------------------------------------------------------

    def heartbeat(self, node_id, now=None):
        """Extend this node's leases for domains it still has in-flight work in."""
        now = now if now is not None else time.time()
        expires = now + self.lease_seconds
        conn = self._conn()
        with self._write_lock:
            conn.execute(
                """
                UPDATE domain_leases SET lease_expires_at=?
                WHERE leased_by=? AND domain IN (
                    SELECT DISTINCT domain FROM candidates
                    WHERE status=? AND claimed_by=?
                )
                """,
                (expires, node_id, STATUS_CLAIMED, node_id),
            )

    def reap_expired(self, now=None):
        """Free work stranded by a dead node; drop the dead leases. Returns freed."""
        now = now if now is not None else time.time()
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE candidates SET status=?, claimed_by=NULL, updated_at=?
                WHERE status=?
                  AND domain NOT IN (
                      SELECT domain FROM domain_leases WHERE lease_expires_at > ?
                  )
                """,
                (STATUS_PENDING, now, STATUS_CLAIMED, now),
            )
            freed = cur.rowcount
            conn.execute("DELETE FROM domain_leases WHERE lease_expires_at <= ?", (now,))
            conn.execute("COMMIT")
        return freed

    def reset_node_claims(self, node_id):
        """Startup recovery for one node: reclaim only its own orphaned claims."""
        conn = self._conn()
        with self._write_lock:
            cur = conn.execute(
                "UPDATE candidates SET status=?, claimed_by=NULL "
                "WHERE status=? AND claimed_by=?",
                (STATUS_PENDING, STATUS_CLAIMED, node_id),
            )
            return cur.rowcount

    def clear(self):
        """Wipe all candidates and leases - used when the UI pushes a brand-new
        batch and wants to start from scratch rather than resume."""
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM candidates")
            conn.execute("DELETE FROM domain_leases")
            conn.execute("COMMIT")

    def lease_snapshot(self, now=None):
        """Current live leases, for the coordinator's /status endpoint."""
        now = now if now is not None else time.time()
        conn = self._conn()
        rows = conn.execute(
            "SELECT domain, leased_by, lease_expires_at FROM domain_leases "
            "WHERE lease_expires_at > ? ORDER BY leased_by, domain",
            (now,),
        ).fetchall()
        return [
            {"domain": r["domain"], "node": r["leased_by"],
             "expires_in": round(r["lease_expires_at"] - now, 1)}
            for r in rows
        ]
