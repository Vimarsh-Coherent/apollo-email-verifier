"""Layer 2 support: MX resolution with caching.

One DNS lookup per domain per run, shared across all worker threads.
"""

import threading

import dns.exception
import dns.resolver


class MXCache:
    def __init__(self, timeout=5.0, nameservers=None):
        self._lock = threading.Lock()
        self._cache = {}
        self._domain_locks = {}

        self._resolver = dns.resolver.Resolver()
        self._resolver.timeout = timeout
        self._resolver.lifetime = timeout
        if nameservers:
            self._resolver.nameservers = list(nameservers)

    def _lock_for(self, domain):
        with self._lock:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = threading.Lock()
            return self._domain_locks[domain]

    def lookup(self, domain):
        """Return {'ok', 'hosts', 'reason', 'implicit'} for a domain.

        `implicit` marks an A-record fallback: RFC 5321 says a domain with no
        MX but with an A record accepts mail at that address.
        """
        domain = (domain or "").strip().lower().rstrip(".")
        if not domain:
            return {"ok": False, "hosts": [], "reason": "empty_domain", "implicit": False}

        with self._lock:
            if domain in self._cache:
                return self._cache[domain]

        with self._lock_for(domain):
            with self._lock:
                if domain in self._cache:
                    return self._cache[domain]
            result = self._resolve(domain)
            with self._lock:
                self._cache[domain] = result
            return result

    def _resolve(self, domain):
        try:
            answers = self._resolver.resolve(domain, "MX")
            records = sorted(
                ((r.preference, str(r.exchange).rstrip(".")) for r in answers),
                key=lambda item: item[0],
            )
            hosts = [host for _, host in records if host and host != "."]
            if hosts:
                return {"ok": True, "hosts": hosts, "reason": "mx", "implicit": False}
            # A single "." exchange is an explicit null MX (RFC 7505).
            return {"ok": False, "hosts": [], "reason": "null_mx", "implicit": False}

        except dns.resolver.NoAnswer:
            pass
        except dns.resolver.NXDOMAIN:
            return {"ok": False, "hosts": [], "reason": "nxdomain", "implicit": False}
        except dns.resolver.NoNameservers:
            return {"ok": False, "hosts": [], "reason": "no_nameservers", "implicit": False}
        except (dns.exception.Timeout, dns.exception.DNSException):
            return {"ok": False, "hosts": [], "reason": "dns_timeout", "implicit": False}

        # No MX record - fall back to the A record.
        try:
            answers = self._resolver.resolve(domain, "A")
            hosts = [str(r) for r in answers]
            if hosts:
                return {"ok": True, "hosts": [domain], "reason": "a_fallback", "implicit": True}
        except dns.exception.DNSException:
            pass

        return {"ok": False, "hosts": [], "reason": "no_mx_