"""The 7 verification layers.

Layer 1  Syntax / normalization     - RFC-ish local+domain sanity, no network
Layer 2  Domain + MX resolution     - domain resolves and accepts mail
Layer 3  Disposable / role filter   - throwaway domains and generic mailboxes
Layer 4  Catch-all detection        - does the domain accept a random mailbox?
Layer 5  SMTP RCPT handshake        - the actual per-address deliverability probe
Layer 6  Greylist-aware retry       - re-run layer 5 on tempfail (handled by runner)
Layer 7  Confidence scoring         - fuse all signals into a verdict

Layers 1-4 are cheap and cached per domain. Layer 5 is the expensive part and
the only one that opens an outbound socket. Layer 6 is the retry loop in
runner.py. Layer 7 is pure aggregation.
"""

import re
import smtplib
import socket
import threading
import time

import dns.exception
import dns.resolver

# --- static reference data ------------------------------------------------

DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "getnada.com",
    "trashmail.com", "sharklasers.com", "maildrop.cc", "dispostable.com",
    "fakeinbox.com", "mailnesia.com", "mohmal.com", "spamgourmet.com",
    "mintemail.com", "tempinbox.com", "emailondeck.com", "burnermail.io",
}

ROLE_LOCAL_PARTS = {
    "admin", "administrator", "info", "support", "sales", "contact", "help",
    "hello", "team", "office", "billing", "accounts", "accounting", "hr",
    "jobs", "careers", "marketing", "media", "press", "legal", "privacy",
    "abuse", "postmaster", "webmaster", "noreply", "no-reply", "donotreply",
    "root", "security", "it", "service", "services", "enquiries", "inquiries",
    "mail", "email", "newsletter", "notifications", "orders", "feedback",
}

# Local part: RFC 5322 dot-atom (the 99.9% real-world case). No quoted strings.
_LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")

# SMTP replies that mean "this mailbox does not exist" vs "try later".
_HARD_BOUNCE_CODES = {550, 551, 553, 554}
_SOFT_CODES = {421, 450, 451, 452, 471}


class LayerResult:
    def __init__(self, layer, name, passed, detail="", data=None, temporary=False):
        self.layer = layer
        self.name = name
        self.passed = passed
        self.detail = detail
        self.data = data or {}
        self.temporary = temporary  # tempfail -> eligible for layer-6 retry

    def as_dict(self):
        return {
            "layer": self.layer,
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "temporary": self.temporary,
            **{f"data_{k}": v for k, v in self.data.items()},
        }


# =========================================================================
# Layer 1 - Syntax
# =========================================================================

def layer1_syntax(email):
    email = (email or "").strip()
    if email.count("@") != 1:
        return LayerResult(1, "syntax", False, "must contain exactly one @")
    local, domain = email.split("@")
    if not local or len(local) > 64:
        return LayerResult(1, "syntax", False, "local part empty or >64 chars")
    if not _LOCAL_RE.match(local):
        return LayerResult(1, "syntax", False, "illegal characters in local part")
    if len(domain) > 255 or "." not in domain:
        return LayerResult(1, "syntax", False, "malformed domain")
    labels = domain.split(".")
    if any(not _LABEL_RE.match(label) for label in labels):
        return LayerResult(1, "syntax", False, "malformed domain label")
    if len(labels[-1]) < 2 or labels[-1].isdigit():
        return LayerResult(1, "syntax", False, "invalid TLD")
    return LayerResult(1, "syntax", True, "well-formed",
                       data={"local": local, "domain": domain.lower()})


# =========================================================================
# Layer 2 - Domain + MX  (cached per domain)
# =========================================================================

class MXCache:
    """Thread-safe per-domain MX cache. Layers 2 and 4 both read it."""

    def __init__(self, timeout):
        self._lock = threading.Lock()
        self._data = {}
        self._resolver = dns.resolver.Resolver()
        self._resolver.timeout = timeout
        self._resolver.lifetime = timeout

    def get_mx(self, domain):
        with self._lock:
            if domain in self._data:
                return self._data[domain]
        result = self._resolve(domain)
        with self._lock:
            self._data[domain] = result
        return result

    def _resolve(self, domain):
        try:
            answers = self._resolver.resolve(domain, "MX")
            hosts = sorted(
                ((r.preference, str(r.exchange).rstrip(".")) for r in answers),
                key=lambda item: item[0],
            )
            mx_hosts = [host for _, host in hosts if host]
            if mx_hosts:
                return {"ok": True, "mx": mx_hosts, "reason": "mx"}
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
                dns.resolver.NoNameservers, dns.exception.DNSException):
            pass
        # No MX: RFC 5321 permits falling back to the A record.
        try:
            self._resolver.resolve(domain, "A")
            return {"ok": True, "mx": [domain], "reason": "a_fallback"}
        except dns.exception.DNSException:
            return {"ok": False, "mx": [], "reason": "no_dns"}


def layer2_domain(domain, mx_cache):
    info = mx_cache.get_mx(domain)
    if not info["ok"]:
        return LayerResult(2, "domain_mx", False, "domain has no MX or A record")
    return LayerResult(2, "domain_mx", True,
                       f"{len(info['mx'])} mail host(s) ({info['reason']})",
                       data={"mx": info["mx"], "primary_mx": info["mx"][0]})


# =========================================================================
# Layer 3 - Disposable / role account
# =========================================================================

def layer3_reputation(local, domain, skip_role):
    if domain in DISPOSABLE_DOMAINS:
        return LayerResult(3, "reputation", False, "disposable domain",
                           data={"disposable": True})
    is_role = local.lower() in ROLE_LOCAL_PARTS
    if is_role and skip_role:
        return LayerResult(3, "reputation", False, "role account (skipped by config)",
                           data={"role": True})
    return LayerResult(3, "reputation", True,
                       "role account" if is_role else "personal mailbox",
                       data={"role": is_role, "disposable": False})


# =========================================================================
# Layer 4 + 5 - SMTP conversation
# =========================================================================

class SMTPProbe:
    """One SMTP conversation against a domain's primary MX.

    Reused for both catch-all detection (layer 4) and the real RCPT (layer 5)
    so we open a single connection and issue multiple RCPT TO commands.
    """

    def __init__(self, mx_host, lease, timeouts):
        self.mx_host = mx_host
        self.lease = lease
        self.timeouts = timeouts

    def _random_local(self):
        # A local part no real mailbox would own. Deterministic-ish but unique
        # enough per call via the clock; catch-all detection only needs "very
        # unlikely to exist".
        stamp = format(int(time.time() * 1000) & 0xFFFFFFFF, "x")
        return f"zz-no-such-user-{stamp}"

    def converse(self, targets, catch_all_probe=True):
        """Run one conversation, testing each address in `targets`.

        Returns dict:
          reachable      - did we complete EHLO/MAIL FROM?
          catch_all      - True/False/None (None = not tested)
          results        - {email: {"code", "message", "status"}}
          error          - transport-level error string, if any
        """
        out = {"reachable": False, "catch_all": None, "results": {}, "error": None}

        # Bind outbound to the leased source IP so rotation actually happens.
        source = (self.lease["ip"], 0)
        # Short timeout for the initial connect (dead MX fails fast); the longer
        # command timeout applies once we're in the conversation.
        server = smtplib.SMTP(timeout=self.timeouts.connect, source_address=source)
        try:
            server.connect(self.mx_host, 25)
            server.timeout = self.timeouts.command
            code, _ = server.ehlo(self.lease["ehlo"])
            if code >= 400:
                code, _ = server.helo(self.lease["ehlo"])
            if code >= 400:
                out["error"] = f"EHLO/HELO rejected ({code})"
                return out

            # Some MX only reveal the real answer after STARTTLS.
            if server.has_extn("starttls"):
                try:
                    server.starttls()
                    server.ehlo(self.lease["ehlo"])
                except (smtplib.SMTPException, socket.error):
                    pass  # continue in the clear; not fatal for a probe

            code, msg = server.mail(self.lease["identity"])
            if code >= 400:
                out["error"] = f"MAIL FROM rejected ({code}: {msg})"
                return out
            out["reachable"] = True

            if catch_all_probe:
                fake = f"{self._random_local()}@{self.lease['domain']}"
                code, msg = server.rcpt(fake)
                out["catch_all"] = code < 300
                out["catch_all_code"] = code

            for email in targets:
                code, msg = server.rcpt(email)
                message = msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg)
                out["results"][email] = {
                    "code": code,
                    "message": message.strip(),
                    "status": _classify_rcpt(code),
                }
            return out
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                socket.timeout, socket.error, OSError) as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        except smtplib.SMTPException as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        finally:
            try:
                server.quit()
            except (smtplib.SMTPException, socket.error):
                try:
                    server.close()
                except Exception:
                    pass


def _classify_rcpt(code):
    if 200 <= code < 300:
        return "accepted"
    if code in _HARD_BOUNCE_CODES or (500 <= code < 600):
        return "rejected"
    if code in _SOFT_CODES or (400 <= code < 500):
        return "tempfail"
    return "unknown"


# =========================================================================
# Layer 7 - Confidence scoring
# =========================================================================

# Verdict buckets.
VERDICT_DELIVERABLE = "deliverable"
VERDICT_RISKY = "risky"            # catch-all or role: accepts, can't prove real
VERDICT_UNDELIVERABLE = "undeliverable"
VERDICT_UNKNOWN = "unknown"        # exhausted retries on tempfail/unreachable


def score(layer_results, rcpt_status, catch_all, is_role, is_known):
    """Fuse layer signals into (verdict, confidence 0-100, reasons)."""
    reasons = []

    for res in layer_results:
        if not res.passed and not res.temporary:
            return VERDICT_UNDELIVERABLE, 0, [f"failed L{res.layer} {res.name}: {res.detail}"]

    if is_known:
        reasons.append("matches an address already provided by the source data")

    if rcpt_status == "rejected":
        return VERDICT_UNDELIVERABLE, 2, reasons + ["MX rejected RCPT (mailbox does not exist)"]

    if rcpt_status in ("tempfail", "unreachable", None):
        base = 25 if is_known else 15
        return VERDICT_UNKNOWN, base, reasons + [f"could not complete SMTP check ({rcpt_status})"]

    if rcpt_status == "accepted":
        if catch_all:
            confidence = 55 if is_known else 40
            reasons.append("domain is catch-all: acceptance does not prove the mailbox exists")
            return VERDICT_RISKY, confidence, reasons
        if is_role:
            reasons.append("role account accepted; deliverable but not a personal mailbox")
            return VERDICT_RISKY, 70, reasons
        confidence = 97 if is_known else 90
        reasons.append("MX accepted RCPT on a non-catch-all domain")
        return VERDICT_DELIVERABLE, confidence, reasons

    return VERDICT_UNKNOWN, 10, reasons + ["unclassified SMTP response"]
