"""Turn a leadership-contacts Excel (one row per company, role columns) into a
deduped, role-filtered set of people, categorised into three phases:

  1. known        - already have a personal email  -> verify the email directly
  2. generate     - no email but company domain known -> generate guesses
  3. needs_domain - no domain at all -> look the domain up (DeepSeek) then generate

Dedup rule: within a company, a person's name appears only once (the highest-
priority role wins), so nobody shows up under two designations.
"""

import json
import urllib.request
from collections import Counter

import pandas as pd

from email_patterns import (
    normalize_domain, FREE_EMAIL_DOMAINS, clean_token, split_name,
    generate_for_person,
)

# (designation label, name column, email column) - order = dedup priority.
ROLES = [
    ("CEO", "CEO", "CEO EMAIL"),
    ("Founder", "Founder", "Founder EMAIL"),
    ("MD", "MD", "MD EMAIL"),
    ("Country Head", "COUNTRY HEAD", "COUNTRY HEAD EMAIL"),
    ("CTO", "CTO", "CTO EMAIL"),
    ("AI Head", "AI HEAD", "AI HEAD EMAIL"),
    ("Marketing Head", "MARKETING HEAD", "MARKETING HEAD EMAIL"),
]

# Generic mailbox local-parts - never a real person; dropped.
ROLE_LOCALPARTS = {
    "sales", "info", "contact", "support", "admin", "hello", "help", "team",
    "office", "enquiry", "enquiries", "inquiry", "inquiries", "marketing", "hr",
    "careers", "jobs", "press", "media", "billing", "accounts", "account",
    "noreply", "no-reply", "donotreply", "webmaster", "postmaster", "abuse",
    "general", "mail", "email", "service", "services", "customercare", "care",
    "reception", "feedback", "hi", "connect", "business",
}


def is_role_email(email):
    if not isinstance(email, str) or "@" not in email:
        return False
    return email.split("@", 1)[0].strip().lower() in ROLE_LOCALPARTS


def _company_domain(row, email_cols):
    """Most common corporate domain across a company's known (non-role) emails."""
    c = Counter()
    for ec in email_cols:
        e = row.get(ec)
        if isinstance(e, str) and "@" in e and not is_role_email(e):
            d = normalize_domain(e)
            if d and d not in FREE_EMAIL_DOMAINS:
                c[d] += 1
    return c.most_common(1)[0][0] if c else ""


def _company_pattern(row, email_cols, domain):
    """Detect the company's email convention from a known email + its owner's
    name, e.g. 'raghuravinutala@..' with name 'Raghu Ravinutala' -> firstlast.
    Returns a template like '{f}.{l}' or None."""
    if not domain:
        return None
    # map each email column to its name column
    name_by_email = {ec: nc for _, nc, ec in ROLES}
    for ec in email_cols:
        e = row.get(ec)
        if not (isinstance(e, str) and "@" in e) or is_role_email(e):
            continue
        if normalize_domain(e) != domain:
            continue
        local = e.split("@", 1)[0].lower()
        nm = row.get(name_by_email.get(ec, ""))
        first, last = split_name({"name": nm if isinstance(nm, str) else ""})
        if not first:
            continue
        f, l, fi, li = first, last, first[:1], last[:1]
        # try the common templates, most specific first
        candidates = [
            ("{f}.{l}", f"{f}.{l}"), ("{f}_{l}", f"{f}_{l}"),
            ("{fi}{l}", f"{fi}{l}"), ("{f}{l}", f"{f}{l}"),
            ("{f}{li}", f"{f}{li}"), ("{fi}.{l}", f"{fi}.{l}"),
            ("{l}.{f}", f"{l}.{f}"), ("{l}{f}", f"{l}{f}"),
            ("{f}", f"{f}"), ("{l}", f"{l}"),
        ]
        for tmpl, built in candidates:
            if built and built == local:
                return tmpl
    return None


def process_excel(file):
    """Read the Excel and return one deduped, role-filtered row per person with
    columns: row_id, company, designation, name, first_name, last_name, email,
    domain, pattern, phase."""
    df = pd.read_excel(file)
    email_cols = [ec for _, _, ec in ROLES if ec in df.columns]
    out = []
    for _, row in df.iterrows():
        company = str(row.get("Company") or "").strip()
        domain = _company_domain(row, email_cols)
        pattern = _company_pattern(row, email_cols, domain)
        seen = set()
        for desig, ncol, ecol in ROLES:
            if ncol not in df.columns:
                continue
            name = row.get(ncol)
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            key = clean_token(name)
            if not key or key in seen:   # same person, another role -> skip
                continue
            seen.add(key)
            known = row.get(ecol)
            known = known.strip().lower() if isinstance(known, str) and "@" in known else ""
            if is_role_email(known):     # drop sales@/info@/...
                known = ""
            first, last = split_name({"name": name})
            out.append({
                "company": company, "designation": desig, "name": name,
                "first_name": first, "last_name": last,
                "email": known, "domain": domain, "pattern": pattern or "",
            })
    r = pd.DataFrame(out)
    if r.empty:
        return r
    r = r.reset_index(drop=True)
    r["row_id"] = r.index

    def phase(row):
        if row["email"]:
            return "known"
        if row["domain"]:
            return "generate"
        return "needs_domain"
    r["phase"] = r.apply(phase, axis=1)
    return r


def known_queue(r):
    """Phase 1: the known personal emails, ready to verify directly."""
    df = r[r["phase"] == "known"]
    return [
        {
            "candidate_email": row["email"],
            "row_id": int(row["row_id"]),
            "id": "", "name": row["name"],
            "domain": normalize_domain(row["email"]),
            "pattern": "known", "rank": 1, "is_known_email": True,
        }
        for _, row in df.iterrows() if row["email"]
    ]


def generated_queue(r, per_person=5):
    """Phase 2: candidate guesses for people whose company domain is known.
    Applies the company's detected pattern first, then the standard catalogue."""
    df = r[(r["phase"] == "generate") & (r["domain"] != "")]
    q = []
    for _, row in df.iterrows():
        person = {
            "name": row["name"], "first_name": row["first_name"],
            "last_name": row["last_name"], "email": "",
            "org_website": row["domain"],
        }
        cands, domain, _ = generate_for_person(
            person, int(row["row_id"]), per_person,
            preferred_pattern=(row["pattern"] or None),
        )
        for c in cands:
            q.append({
                "candidate_email": c["email"], "row_id": int(row["row_id"]),
                "id": "", "name": row["name"], "domain": domain,
                "pattern": c["pattern"], "rank": c["rank"],
                "is_known_email": False,
            })
    return q


# ----------------------------------------------------------------------
# Phase 3: fill missing company domains via the DeepSeek API.
# ----------------------------------------------------------------------
def deepseek_find_domains(companies, api_key, batch=20, on_progress=None):
    """Ask DeepSeek for the primary email domain of each company name.
    Returns {company: domain}. Best-effort - LLM answers can be wrong, so
    results are only used to *generate* guesses (which are then SMTP-verified)."""
    result = {}
    companies = [c for c in companies if c]
    url = "https://api.deepseek.com/chat/completions"
    for i in range(0, len(companies), batch):
        chunk = companies[i:i + batch]
        prompt = (
            "For each company below, give ONLY its primary corporate email "
            "domain (the part after @ in employee emails), or \"\" if unknown. "
            "Reply as strict JSON mapping company name -> domain, nothing else.\n\n"
            + "\n".join(f"- {c}" for c in chunk)
        )
        body = json.dumps({
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            content = content[content.find("{"): content.rfind("}") + 1]
            mapping = json.loads(content)
            for k, v in mapping.items():
                d = normalize_domain(str(v))
                if d and d not in FREE_EMAIL_DOMAINS:
                    result[k] = d
        except Exception:
            pass  # skip a failed batch; those companies just stay domain-less
        if on_progress:
            on_progress(min(i + batch, len(companies)), len(companies))
    return result
