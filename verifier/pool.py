"""IP + sender-identity rotation pool with per-domain and per-IP throttling.

5 IPs x 10 identities. The pool's job is to hand out an (ip, identity) pair
that is currently allowed to probe a given domain, while enforcing:
  - one live socket per domain at a time
  - a minimum spacing between probes to the same domain
  - hourly caps per domain and per IP
  - IP rotation on retry, so a tempfail is retried from a fresh address
All state is in-memory and thread-safe; the SQLite layer handles durability.
"""

import threading
import time
from collections import defaultdict, deque


class _Window:
    """Sliding 1-hour counter."""

    def __init__(self):
        self.events = deque()

    def prune(self, now):
        cutoff = now - 3600
        while self.events and self.events[0] < cutoff:
            self.events.popleft()

    def count(self, now):
        self.prune(now)
        return len(self.events)

    def add(self, now):
        self.events.append(now)


class RotationPool:
    def __init__(self, config, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

        self.ips = list(config.source_ips)
        self.identities = list(config.sender_identities)
        self._rr_ip = 0
        self._rr_identity = 0

        # Throttling state.
        self._domain_last_probe = {}
        self._domain_active = defaultdict(int)
        self._domain_window = defaultdict(_Window)
        self._ip_window = defaultdict(_Window)
        self._ip_domain_window = defaultdict(_Window)  # keyed (ip, domain)
        self._total_active = 0

    def _min_delay(self, strict):
        rl = self.config.rate_limits
        return rl.min_delay_strict_domain if strict else rl.min_delay_per_domain

    def _domain_hour_cap(self, strict):
        rl = self.config.rate_limits
        return rl.max_probes_strict_domain_hour if strict else rl.max_probes_per_domain_hour

    def _pick_ip(self, domain, now, avoid=None):
        """Round-robin an IP that is under its caps and not the avoided one."""
        rl = self.config.rate_limits
        n = len(self.ips)
        for offset in range(n):
            ip = self.ips[(self._rr_ip + offset) % n]
            if avoid is not None and ip == avoid and n > 1:
                continue
            if self._ip_window[ip].count(now) >= rl.max_probes_per_ip_hour:
                continue
            if self._ip_domain_window[(ip, domain)].count(now) >= rl.max_probes_per_ip_domain_hour:
                continue
            self._rr_ip = (self._rr_ip + offset + 1) % n
            return ip
        return None

    def _pick_identity(self):
        identity = self.identities[self._rr_identity % len(self.identities)]
        self._rr_identity += 1
        return identity

    def acquire(self, domain, strict, avoid_ip=None, deadline=None):
        """Block until a slot for `domain` is free, then return a lease dict.

        Returns None if `deadline` (monotonic) passes first.
        """
        min_delay = self._min_delay(strict)
        domain_cap = self._domain_hour_cap(strict)
        rl = self.config.rate_limits

        with self._cv:
            while True:
                now = self.clock()

                blockers = []
                if self._total_active >= rl.max_concurrent_total:
                    blockers.append(None)
                if self._domain_active[domain] >= rl.max_concurrent_per_domain:
                    blockers.append(None)

                wait_for = 0.0
                last = self._domain_last_probe.get(domain)
                if last is not None:
                    elapsed = now - last
                    if elapsed < min_delay:
                        wait_for = max(wait_for, min_delay - elapsed)

                over_domain_hour = self._domain_window[domain].count(now) >= domain_cap
                ip = None
                if not blockers and wait_for == 0 and not over_domain_hour:
                    ip = self._pick_ip(domain, now, avoid=avoid_ip)

                if not blockers and wait_for == 0 and not over_domain_hour and ip is not None:
                    identity = self._pick_identity()
                    self._domain_active[domain] += 1
                    self._total_active += 1
                    self._domain_last_probe[domain] = now
                    self._domain_window[domain].add(now)
                    self._ip_window[ip].add(now)
                    self._ip_domain_window[(ip, domain)].add(now)
                    return {
                        "ip": ip,
                        "identity": identity,
                        "ehlo": self.config.ehlo_for(ip),
                        "domain": domain,
                    }

                # Work out how long to sleep before re-checking.
                timeout = 1.0
                if wait_for:
                    timeout = min(timeout, wait_for)
                if over_domain_hour or ip is None:
                    # Hourly caps only free up on the minute scale; poll slowly.
                    timeout = 2.0

                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        return None
                    timeout = min(timeout, remaining)

                self._cv.wait(timeout=timeout)

    def release(self, lease):
        with self._cv:
            domain = lease["domain"]
            if self._domain_active[domain] > 0:
                self._domain_active[domain] -= 1
            if self._total_active > 0:
                self._total_active -= 1
            self._cv.notify_all()

    def snapshot(self):
        with self._lock:
            now = self.clock()
            return {
                "total_active": self._total_active,
                "ip_load": {ip: self._ip_window[ip].count(now) for ip in self.ips},
                "active_domains": {d: n for d, n in self._domain_active.items() if n},
            }
