"""Email permutation engine.

Generates N candidate email addresses per person using randomized, frequency
weighted corporate naming patterns. Output is designed to be fed into the
SMTP verification pipeline (see the "long" format produced by build_candidates).
"""

import hashlib
import random
import re
import unicodedata
from urllib.parse import urlparse

# Pattern catalogue. Placeholders:
#   {f}  = full first name      {fi} = first initial
#   {l}  = full last name       {li} = last initial
# Weights approximate real-world frequency of each convention, so the common
# patterns almost always get picked while rarer ones still surface for variety.
EMAIL_PATTERNS = [
    ("first.last",  "{f}.{l}",  12),
    ("first",       "{f}",       9),
    ("flast",       "{fi}{l}",   9),
    ("firstlast",   "{f}{l}",    7),
    ("first_last",  "{f}_{l}",   5),
    ("f.last",      "{fi}.{l}",  5),
    ("firstl",      "{f}{li}",   4),
    ("first.l",     "{f}.{li}",  3),
    ("last",        "{l}",       3),
    ("last.first",  "{l}.{f}",   3),
    ("lastfirst",   "{l}{f}",    2),
    ("lastf",       "{l}{fi}",   2),
    ("fl",          "{fi}{li}",  2),
    ("f_last",      "{fi}_{l}",  2),
    ("first-last",  "{f}-{l}",   2),
    ("f.l",         "{fi}.{li}", 1),
    ("last_first",  "{l}_{f}",   1),
    ("last-first",  "{l}-{f}",   1),
]

# Permutations against these are worthless (and probing them gets an IP blocked).
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "gmx.com", "gmx.de", "mail.com", "zoho.com", "yandex.com", "yandex.ru",
    "qq.com", "163.com", "126.com", "naver.com", "rediffmail.com",
    "hotmail.fr", "orange.fr", "web.de", "t-online.de", "comcast.net",
    "verizon.net", "sbcglobal.net", "att.net", "btinternet.com", "free.fr",
}

# Apollo puts a placeholder in the `email` field for contacts you have NOT
# unlocked, e.g. "email_not_unlocked@domain.com". Its domain ("domain.com") is
# not a real company domain, so any permutation built on it is junk. Detect
# these and ignore them, falling back to the org website instead.
INVALID_DOMAINS = {
    "domain.com", "email.com", "example.com", "notunlocked.com", "unlocked.com",
}
PLACEHOLDER_EMAIL_LOCALPARTS = {
    "email_not_unlocked", "email_not_found", "notunlocked", "not_unlocked",
}

# Tokens that are titles/suffixes rather than name parts.
_NAME_NOISE = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam",
    "jr", "sr", "ii", "iii", "iv", "v",
    "phd", "md", "mba", "cpa", "esq", "cfa", "pmp", "rn", "dds", "do",
}


def clean_token(value):
    """Lowercase, strip accents, drop everything that is not a-z0-9."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def split_name(row):
    """Resolve (first, last) as cleaned tokens, falling back to the full name."""
    first = str(row.get("first_name") or "").strip()
    last = str(row.get("last_name") or "").strip()

    if not first or not last:
        parts = [p for p in str(row.get("name") or "").split()
                 if p.lower().strip(".,") not in _NAME_NOISE]
        if not first and parts:
            first = parts[0]
        if not last and len(parts) > 1:
            last = parts[-1]

    return clean_token(first), clean_token(last)


def normalize_domain(value):
    """Pull a bare domain out of a URL, hostname, or email address."""
    if not value:
        return ""
    text = str(value).strip().lower()
    if not text or text in ("nan", "none"):
        return ""
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    if "://" not in text:
        text = "//" + text
    host = urlparse(text).netloc or ""
    host = host.split(":")[0].strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    # Must look like an actual domain.
    if "." not in host or not re.match(r"^[a-z0-9.\-]+$", host):
        return ""
    # Reject Apollo's placeholder / obviously-fake domains.
    if host in INVALID_DOMAINS:
        return ""
    return host


def resolve_domain(row):
    """Pick the best company domain for a person.

    Returns (domain, source). An existing corporate email is the strongest
    signal; the org website is the fallback. Free-mail domains, and Apollo's
    "email_not_unlocked@domain.com" placeholders, are rejected.
    """
    raw_email = str(row.get("email") or "").strip().lower()
    local, _, raw_domain = raw_email.partition("@")
    is_placeholder = (local in PLACEHOLDER_EMAIL_LOCALPARTS
                      or raw_domain in INVALID_DOMAINS)

    # Ignore the placeholder entirely so it can't become the domain.
    known = "" if is_placeholder else normalize_domain(raw_email)
    if known and known not in FREE_EMAIL_DOMAINS:
        return known, "known_email"

    site = normalize_domain(row.get("org_website", ""))
    if site and site not in FREE_EMAIL_DOMAINS:
        return site, "org_website"

    if known:
        return "", "free_email_only"
    return "", "no_domain"


def _weighted_sample(patterns, count, rng):
    """Weighted sampling without replacement — order is the pick order."""
    pool = list(patterns)
    picked = []
    while pool and len(picked) < count:
        total = sum(w for _, _, w in pool)
        threshold = rng.uniform(0, total)
        running = 0.0
        for index, (name, template, weight) in enumerate(pool):
            running += weight
            if running >= threshold:
                picked.append((name, template))
                pool.pop(index)
                break
        else:
            picked.append((pool[-1][0], pool[-1][1]))
            pool.pop()
    return picked


def _seed_for(row, index, salt):
    """Stable per-person seed so a rerun reproduces the same picks."""
    key = "|".join([
        str(row.get("id") or ""),
        str(row.get("person_id") or ""),
        str(row.get("name") or ""),
        str(index),
        str(salt),
    ])
    return int(hashlib.md5(key.encode("utf-8")).hexdigest()[:16], 16)


def generate_for_person(row, index, count, salt=0, reproducible=True):
    """Return a list of {email, pattern, rank, ...} dicts for one person.

    Fewer than `count` items come back when the name cannot fill that many
    distinct patterns (e.g. a first name with no surname).
    """
    first, last = split_name(row)
    domain, domain_source = resolve_domain(row)

    if not domain or not first:
        return [], domain, domain_source

    values = {"f": first, "l": last, "fi": first[:1], "li": last[:1]}

    # Drop patterns that need a surname when we do not have one.
    usable = EMAIL_PATTERNS if last else [
        p for p in EMAIL_PATTERNS if "{l}" not in p[1] and "{li}" not in p[1]
    ]

    rng = random.Random(_seed_for(row, index, salt)) if reproducible else random.Random()

    known_email = str(row.get("email") or "").strip().lower()
    candidates = []
    seen = set()

    # Over-sample so duplicates collapsing (e.g. "first" == "flast" for short
    # names) still leaves us with `count` distinct addresses where possible.
    for pattern_name, template in _weighted_sample(usable, len(usable), rng):
        local = template.format(**values).strip("._-")
        local = re.sub(r"[._-]{2,}", ".", local)
        if not local or local in seen:
            continue
        seen.add(local)
        email = f"{local}@{domain}"
        candidates.append({
            "pattern": pattern_name,
            "email": email,
            "is_known": email == known_email,
        })
        if len(candidates) >= count:
            break

    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank

    return candidates, domain, domain_source


def build_candidates(df, count, reproducible=True, salt=0):
    """Generate candidates for every row of `df`.

    Returns (wide_records, long_records, stats):
      wide  - one record per person, generated_email_1..N columns (review/export)
      long  - one record per candidate email (feeds SMTP verification)
    """
    wide_records = []
    long_records = []
    stats = {
        "people_total": len(df),
        "people_with_domain": 0,
        "people_skipped_no_domain": 0,
        "people_skipped_free_email": 0,
        "people_skipped_no_name": 0,
        "candidates_total": 0,
        "domains": set(),
    }

    for index, row in enumerate(df.to_dict(orient="records")):
        candidates, domain, domain_source = generate_for_person(
            row, index, count, salt=salt, reproducible=reproducible
        )

        record = {
            "row_id": index,
            "id": row.get("id", ""),
            "name": row.get("name", ""),
            "first_name": row.get("first_name", ""),
            "last_name": row.get("last_name", ""),
            "title": row.get("title", ""),
            "org_name": row.get("org_name", "") or row.get("organization_name", ""),
            "known_email": row.get("email", ""),
            "domain": domain,
            "domain_source": domain_source,
            "generated_count": len(candidates),
        }
        for slot in range(1, count + 1):
            record[f"generated_email_{slot}"] = (
                candidates[slot - 1]["email"] if slot <= len(candidates) else ""
            )
            record[f"pattern_{slot}"] = (
                candidates[slot - 1]["pattern"] if slot <= len(candidates) else ""
            )
        wide_records.append(record)

        if candidates:
            stats["people_with_domain"] += 1
            stats["domains"].add(domain)
            stats["candidates_total"] += len(candidates)
            for candidate in candidates:
                long_records.append({
                    "row_id": index,
                    "id": row.get("id", ""),
                    "name": row.get("name", ""),
                    "first_name": row.get("first_name", ""),
                    "last_name": row.get("last_name", ""),
                    "title": row.get("title", ""),
                    "org_name": record["org_name"],
                    "domain": domain,
                    "domain_source": domain_source,
                    "candidate_email": candidate["email"],
                    "pattern": candidate["pattern"],
                    "rank": candidate["rank"],
                    "is_known_email": candidate["is_known"],
                    "linkedin_url": row.get("linkedin_url", ""),
                })
        elif domain_source == "free_email_only":
            stats["people_skipped_free_email"] += 1
        elif not split_name(row)[0]:
            stats["people_skipped_no_name"] += 1
        else:
            stats["people_skipped_no_domain"] += 1

    stats["unique_domains"] = len(stats.pop("domains"))
    return wide_records, long_records, stats
