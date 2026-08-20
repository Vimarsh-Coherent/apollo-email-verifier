"""Orchestrates the 7 layers across a thread pool with the rotation pool.

Flow per candidate:
  L1 syntax -> L3 reputation (both cheap, no network)
  L2 domain/MX (cached per domain)
  acquire (ip, identity) lease from the pool  ->  L4 catch-all + L5 RCPT
    (one SMTP conversation does both)
  on tempfail: schedule L6 retry with backoff + IP rotation
  L7 score -> persist verdict
"""

import threading
import time

from .layers import (
    MXCache, SMTPProbe, layer1_syntax, layer2_domain, layer3_reputation, score,
    VERDICT_UNDELIVERABLE, VERDICT_UNKNOWN,
)
from .state import StateStore, STATUS_DONE


class Verifier:
    def __init__(self, config, pool, store=None, mx_cache=None, on_event=None,
                 offline=False, keep_alive=False):
        self.config = config
        self.pool = pool
        self.store = store or StateStore(config.state_db)
        self.mx_cache = mx_cache or MXCache(config.timeouts.dns)
        self.on_event = on_event or (lambda *a, **k: None)
        # offline=True runs layers 1-3 only: no SMTP sockets, no IP binding.
        self.offline = offline
        # keep_alive=True: never exit when the queue is empty - keep polling for
        # new work. This is what lets a VPS worker sit idle waiting for the UI to
        # push a fresh batch to the coordinator, instead of shutting down.
        self.keep_alive = keep_alive
        self._stop = threading.Event()

        # Cache catch-all status per domain: probing it once per domain is
        # plenty and saves a RCPT on every subsequent address.
        self._catch_all = {}
        self._catch_all_lock = threading.Lock()

    def stop(self):
        self._stop.set()

    def run(self, candidates=None):
        """Seed (optional) and process until no open work remains."""
        # Recover any rows a previous crash left mid-flight.
        recovered = self.store.reset_stale_claims()
        if recovered:
            self.on_event("recover", recovered=recovered)

        if candidates:
            added = self.store.seed(candidates)
            self.on_event("seed", added=added, total=len(candidates))

        threads = [
            threading.Thread(target=self._worker, name=f"verify-{i}", daemon=True)
            for i in range(self.config.workers)
        ]
        for t in threads:
            t.start()

        try:
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(timeout=0.5)
                if self._stop.is_set():
                    break
        finally:
            self._stop.set()
            for t in threads:
                t.join(timeout=self.config.timeouts.command + 5)

        return self.store.counts()

    # ------------------------------------------------------------------

    def _worker(self):
        while not self._stop.is_set():
            batch = self.store.claim_batch(limit=1)
            if not batch:
                if not self.store.has_open_work() and not self.keep_alive:
                    return
                # Either work is waiting on a retry backoff, or we're in
                # keep-alive mode idling for the UI to push a new batch. Poll.
                self._stop.wait(timeout=2.0)
                continue
            for row in batch:
                if self._stop.is_set():
                    return
                # A bug while processing ONE address must never kill the worker
                # thread (and cascade into the whole worker exiting). Mark that
                # address unknown and keep going.
                try:
                    self._process(row)
                except Exception as exc:
                    self.on_event("error", email=row.get("candidate_email"),
                                  detail=str(exc))
                    try:
                        self.store.mark_error(
                            row["candidate_email"],
                            [f"worker exception: {type(exc).__name__}: {exc}"],
                            (row.get("attempts") or 0) + 1, None,
                        )
                    except Exception:
                        pass

    def _process(self, row):
        email = row["candidate_email"]
        attempts = row["attempts"]
        is_known = bool(row["is_known"])

        # Skip remaining candidates for a person already confirmed deliverable.
        if self.config.stop_on_first_hit and self.store.person_has_hit(row["row_id"]):
            self.store.mark_done(
                email, "skipped", 0, None, None,
                ["another address for this person already verified deliverable"],
                [], attempts, None,
            )
            self.on_event("skip", email=email)
            return

        layer_results = []

        # ---- Layers 1 + 3 (no network) ----
        l1 = layer1_syntax(email)
        layer_results.append(l1.as_dict())
        if not l1.passed:
            self._finalize_fail(email, layer_results, l1.detail, attempts)
            return
        local = l1.data["local"]
        domain = l1.data["domain"]

        l3 = layer3_reputation(local, domain, self.config.skip_role_accounts)
        layer_results.append(l3.as_dict())
        if not l3.passed:
            self._finalize_fail(email, layer_results, l3.detail, attempts)
            return
        is_role = l3.data.get("role", False)

        # ---- Layer 2 (cached DNS) ----
        l2 = layer2_domain(domain, self.mx_cache)
        layer_results.append(l2.as_dict())
        if not l2.passed:
            self._finalize_fail(email, layer_results, l2.detail, attempts)
            return
        primary_mx = l2.data["primary_mx"]
        strict = self.config.is_strict_mx(primary_mx)

        # Offline / dry-run: layers 1-3 passed and the domain accepts mail, but
        # we do not open a socket. Verdict is deliberately "unknown" - passing
        # syntax + MX is necessary, not sufficient, for deliverability.
        if self.offline:
            self.store.mark_done(
                email, VERDICT_UNKNOWN, 20, None, None,
                ["offline run: passed syntax/reputation/MX; SMTP not checked"],
                layer_results, attempts + 1, None,
            )
            self.on_event("done", email=email, verdict=VERDICT_UNKNOWN, confidence=20)
            return

        # ---- Layers 4 + 5 (one SMTP conversation) ----
        avoid_ip = row["last_ip"] if (attempts and self.config.retry.rotate_ip_on_retry) else None
        deadline = time.monotonic() + 120
        lease = self.pool.acquire(domain, strict, avoid_ip=avoid_ip, deadline=deadline)
        if lease is None:
            self._schedule_retry(email, attempts, None, "pool acquire timed out")
            return

        try:
            need_catch_all, cached_ca = self._catch_all_needed(domain)
            probe = SMTPProbe(primary_mx, lease, self.config.timeouts,
                              bind_source=self.config.bind_source_ip,
                              use_starttls=self.config.use_starttls)
            result = probe.converse([email], catch_all_probe=need_catch_all)
        finally:
            self.pool.release(lease)

        used_ip = lease["ip"]

        if result["error"] or not result["reachable"]:
            detail = result["error"] or "MX unreachable"
            self._schedule_retry(email, attempts, used_ip, detail)
            return

        if need_catch_all and result.get("catch_all") is not None:
            self._record_catch_all(domain, result["catch_all"])
            catch_all = result["catch_all"]
        else:
            catch_all = cached_ca

        rcpt = result["results"].get(email, {})
        rcpt_status = rcpt.get("status", "unknown")
        rcpt_code = rcpt.get("code")
        layer_results.append({
            "layer": 5, "name": "smtp_rcpt", "passed": rcpt_status == "accepted",
            "detail": f"{rcpt_code} {rcpt.get('message','')}".strip(),
            "temporary": rcpt_status == "tempfail",
        })
        layer_results.append({
            "layer": 4, "name": "catch_all",
            "passed": catch_all is False, "detail": f"catch_all={catch_all}",
        })

        # ---- Layer 6: retry tempfails ----
        if rcpt_status in ("tempfail", "unknown"):
            self._schedule_retry(email, attempts, used_ip,
                                 f"RCPT {rcpt_code}: {rcpt.get('message','')}",
                                 layer_results=layer_results)
            return

        # ---- Layer 7: score + persist ----
        verdict, confidence, reasons = score(
            [], rcpt_status, catch_all, is_role, is_known
        )
        self.store.mark_done(
            email, verdict, confidence, rcpt_code, catch_all,
            reasons, layer_results, attempts + 1, used_ip,
        )
        self.on_event("done", email=email, verdict=verdict, confidence=confidence)

    # ------------------------------------------------------------------

    def _catch_all_needed(self, domain):
        with self._catch_all_lock:
            if domain in self._catch_all:
                return False, self._catch_all[domain]
        return True, None

    def _record_catch_all(self, domain, value):
        with self._catch_all_lock:
            self._catch_all.setdefault(domain, value)

    def _schedule_retry(self, email, attempts, used_ip, detail, layer_results=None):
        policy = self.config.retry
        attempts_done = attempts + 1
        if attempts_done >= policy.max_attempts:
            # Give up: exhausted retries -> unknown, not a false negative.
            reasons = [f"exhausted {policy.max_attempts} attempts", detail]
            self.store.mark_done(
                email, VERDICT_UNKNOWN, 15, None, None,
                reasons, layer_results or [], attempts_done, used_ip,
            )
            self.on_event("give_up", email=email, detail=detail)
            return
        idx = min(attempts, len(policy.backoff_seconds) - 1)
        delay = policy.backoff_seconds[idx]
        self.store.mark_retry(email, attempts_done, time.time() + delay, used_ip)
        self.on_event("retry", email=email, attempt=attempts_done, delay=delay, detail=detail)

    def _finalize_fail(self, email, layer_results, detail, attempts):
        self.store.mark_done(
            email, VERDICT_UNDELIVERABLE, 0, None, None,
            [detail], layer_results, attempts + 1, None,
        )
        self.on_event("done", email=email, verdict=VERDICT_UNDELIVERABLE, confidence=0)
