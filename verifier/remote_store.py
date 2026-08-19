"""HTTP client that makes a worker VPS talk to the coordinator.

Drop-in for StateStore: exposes the same methods the runner calls, but each one
is an HTTP round-trip to the coordinator instead of a local SQLite query. So
runner.py is unchanged - it just gets this store injected in coordinator mode.

The node_id is baked in at construction and attached to claim/heartbeat/reset
calls, so the coordinator knows which VPS is asking. A background thread sends
heartbeats to keep this node's domain leases alive; if this process dies the
heartbeats stop and the coordinator reclaims its work.

Uses only the standard library (urllib) - no new dependency.
"""

import json
import threading
import time
import urllib.error
import urllib.request


class CoordinatorError(RuntimeError):
    pass


class RemoteStore:
    def __init__(self, base_url, token, node_id, lease_seconds=300.0,
                 timeout=30.0, on_event=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.node_id = node_id
        self.timeout = timeout
        self.on_event = on_event or (lambda *a, **k: None)
        # Heartbeat comfortably more often than the lease expires.
        self._hb_interval = max(5.0, float(lease_seconds) / 3.0)
        self._stop = threading.Event()
        self._hb_thread = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _request(self, method, path, payload=None):
        url = self.base_url + path
        data = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        last_err = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                # 4xx are our fault (bad token/route) - don't retry.
                detail = exc.read().decode("utf-8", "replace")
                if 400 <= exc.code < 500:
                    raise CoordinatorError(f"{exc.code} {detail}") from exc
                last_err = exc
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                last_err = exc
            # Transient: brief backoff, then retry. A coordinator restart or a
            # network blip shouldn't kill a worker.
            self._stop.wait(min(2.0 * (attempt + 1), 8.0))
        raise CoordinatorError(f"coordinator unreachable at {url}: {last_err}")

    # ------------------------------------------------------------------
    # Heartbeat lifecycle
    # ------------------------------------------------------------------

    def start_heartbeat(self):
        if self._hb_thread is not None:
            return
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="hb", daemon=True
        )
        self._hb_thread.start()

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            try:
                self._request("POST", "/heartbeat", {"node_id": self.node_id})
            except CoordinatorError as exc:
                self.on_event("hb_fail", detail=str(exc))
            self._stop.wait(self._hb_interval)

    def close(self):
        self._stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=self._hb_interval + 2)

    # ------------------------------------------------------------------
    # StateStore interface (what runner.py / verify_emails.py call)
    # ------------------------------------------------------------------

    def seed(self, candidates):
        # Coordinator owns the queue and seeds it from its own input file, so a
        # worker never seeds. Present only to satisfy the interface.
        return 0

    def reset_stale_claims(self):
        return self._request("POST", "/reset_stale_claims",
                             {"node_id": self.node_id}).get("reset", 0)

    def claim_batch(self, limit, now=None):
        resp = self._request("POST", "/claim",
                            {"node_id": self.node_id, "limit": limit})
        return resp.get("rows", [])

    def person_has_hit(self, row_id):
        return self._request("POST", "/person_has_hit",
                            {"row_id": row_id}).get("hit", False)

    def has_open_work(self, now=None):
        return self._request("POST", "/has_open_work", {}).get("open", False)

    def mark_done(self, email, verdict, confidence, rcpt_code, catch_all,
                  reasons, layers, attempts, last_ip):
        self._request("POST", "/mark_done", {
            "email": email, "verdict": verdict, "confidence": confidence,
            "rcpt_code": rcpt_code, "catch_all": catch_all, "reasons": reasons,
            "layers": layers, "attempts": attempts, "last_ip": last_ip,
        })

    def mark_retry(self, email, attempts, next_attempt_at, last_ip):
        self._request("POST", "/mark_retry", {
            "email": email, "attempts": attempts,
            "next_attempt_at": next_attempt_at, "last_ip": last_ip,
        })

    def mark_error(self, email, reasons, attempts, last_ip):
        self._request("POST", "/mark_error", {
            "email": email, "reasons": reasons,
            "attempts": attempts, "last_ip": last_ip,
        })

    def counts(self):
        return self._request("GET", "/counts")

    def export_rows(self):
        return self._request("GET", "/export").get("rows", [])
