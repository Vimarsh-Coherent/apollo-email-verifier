"""Configuration for the 7-layer verification system.

Sized for the current deployment: 5 source IPs, 10 sender identities.
Every knob that affects how hard we hit a remote MX lives here.
"""

import json
import os
from dataclasses import dataclass, field, asdict


@dataclass
class RateLimits:
    # Never open two sockets to the same domain at once - the single fastest
    # way to look like an attacker.
    max_concurrent_per_domain: int = 1

    # Seconds to wait between probes to the same domain, regardless of IP.
    min_delay_per_domain: float = 6.0

    # Big providers greylist and tarpit aggressively; slow right down.
    min_delay_strict_domain: float = 25.0

    # Hard ceiling of probes to one domain per hour across the whole pool.
    max_probes_per_domain_hour: int = 40
    max_probes_strict_domain_hour: int = 12

    # Per-IP ceilings.
    max_probes_per_ip_hour: int = 300
    max_probes_per_ip_domain_hour: int = 15

    # Global socket concurrency across all workers.
    max_concurrent_total: int = 10


@dataclass
class Timeouts:
    dns: float = 5.0
    connect: float = 12.0
    command: float = 15.0


@dataclass
class RetryPolicy:
    # Layer 6. Greylisting typically clears after 60-900s.
    max_attempts: int = 3
    backoff_seconds: tuple = (90, 420, 1200)
    # Always retry a tempfail from a different IP than the one that got it.
    rotate_ip_on_retry: bool = True


@dataclass
class Config:
    # ---- Pool: 5 IPs, 10 identities -> 50 (ip, identity) combinations -------
    source_ips: list = field(default_factory=list)
    sender_identities: list = field(default_factory=list)

    # EHLO name. MUST be a hostname with a valid A record and matching PTR for
    # each source IP, or well-run MX servers will reject the conversation
    # outright. Set one per IP in ehlo_by_ip when your rDNS differs per IP.
    ehlo_hostname: str = "mail.example.com"
    ehlo_by_ip: dict = field(default_factory=dict)

    rate_limits: RateLimits = field(default_factory=RateLimits)
    timeouts: Timeouts = field(default_factory=Timeouts)
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    # Stop probing a person's remaining candidates once one is confirmed
    # deliverable on a non-catch-all domain. Large cost saver at 5-7 per person.
    stop_on_first_hit: bool = True

    # Skip the SMTP layers entirely for role accounts (info@, sales@ ...).
    skip_role_accounts: bool = False

    # Worker threads. Keep at or below max_concurrent_total.
    workers: int = 10

    # Bind each outbound probe to its source IP (real rotation). Set False when
    # the provider blocks IPv4 port 25 but allows IPv6 (e.g. Hostinger): binding
    # forces the blocked IPv4 path, so we let the OS route over IPv6 instead.
    bind_source_ip: bool = True

    # Attempt STARTTLS during the probe. Off by default: TLS is unnecessary for a
    # RCPT check and a failed negotiation makes the MX drop the whole
    # conversation before we reach RCPT.
    use_starttls: bool = False

    # SQLite file holding resumable per-candidate state.
    state_db: str = "verification_state.db"

    # Domains that need the conservative rate limits.
    strict_mx_patterns: list = field(default_factory=lambda: [
        "google.com", "googlemail.com", "outlook.com", "protection.outlook.com",
        "hotmail.com", "yahoodns.net", "icloud.com", "mimecast.com",
        "pphosted.com", "barracudanetworks.com", "messagelabs.com",
        "trendmicro.com", "sophos.com", "fireeyecloud.com",
    ])

    @classmethod
    def load(cls, path, require_pool=True):
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)

        cfg = cls()
        for key, value in raw.items():
            if key == "rate_limits":
                cfg.rate_limits = RateLimits(**value)
            elif key == "timeouts":
                cfg.timeouts = Timeouts(**value)
            elif key == "retry":
                policy = dict(value)
                if "backoff_seconds" in policy:
                    policy["backoff_seconds"] = tuple(policy["backoff_seconds"])
                cfg.retry = RetryPolicy(**policy)
            elif hasattr(cfg, key):
                setattr(cfg, key, value)
            else:
                raise ValueError(f"Unknown config key: {key}")
        cfg.validate(require_pool=require_pool)
        return cfg

    def validate(self, require_pool=True):
        # Pool checks only matter for a live run; an offline/dry run opens no
        # sockets, so it can run before the VPS or its IPs even exist.
        if require_pool:
            if not self.source_ips:
                raise ValueError(
                    "source_ips is empty. Add the 5 VPS IPs - without explicit "
                    "binding every probe leaves from the default interface and "
                    "you lose the whole point of the rotation pool."
                )
            if not self.sender_identities:
                raise ValueError("sender_identities is empty. Add the 10 MAIL FROM addresses.")
            if self.ehlo_hostname == "mail.example.com" and not self.ehlo_by_ip:
                raise ValueError(
                    "ehlo_hostname is still the placeholder. Set it to a real host "
                    "whose forward and reverse DNS match your source IPs."
                )
        if self.workers > self.rate_limits.max_concurrent_total:
            raise ValueError(
                f"workers ({self.workers}) exceeds "
                f"max_concurrent_total ({self.rate_limits.max_concurrent_total})"
            )

    def ehlo_for(self, ip):
        return self.ehlo_by_ip.get(ip, self.ehlo_hostname)

    def is_strict_mx(self, mx_host):
        host = (mx_host or "").lower()
        return any(pattern in host for pattern in self.strict_mx_patterns)

    def to_dict(self):
        return asdict(self)


def default_config_path():
    return os.environ.get("VERIFIER_CONFIG", "verifier_config.json")
