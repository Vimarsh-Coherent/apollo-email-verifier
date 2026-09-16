"""Bounce checking: SEND a real email to each address and detect bounces by
reading the sender's own inbox over IMAP.

This ACTUALLY SENDS email (unlike verifier/, which stops at RCPT and never sends
DATA). High bounce volume can get the sending account suspended, so use a
DEDICATED account and vetted lists only. Credentials are read from Streamlit
Secrets by the caller and passed in here - nothing is stored or hardcoded.
"""

import email as emaillib
import imaplib
import random
import re
import smtplib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# From-addresses / subjects that mark a message as a bounce / NDR.
_BOUNCE_FROM = ("mailer-daemon", "postmaster", "mail-daemon")
_BOUNCE_SUBJ = ("delivery status", "undeliver", "failure notice", "returned mail",
                "mail delivery failed", "delivery has failed", "could not be delivered",
                "delivery incomplete")


def send_probes(smtp_host, smtp_port, sender, app_password, targets, subject, body,
                delay=2.0, limit=None, use_ssl=False, on_progress=None):
    """Send `subject`/`body` to each target. Returns {email: 'sent' | 'error: ...'}.
    `delay` seconds between sends; `limit` caps the count (volume safety)."""
    targets = list(dict.fromkeys(a.strip() for a in targets if a and "@" in a))
    if limit:
        targets = targets[:limit]
    results = {}

    if use_ssl:
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
    else:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
    try:
        server.ehlo()
        if not use_ssl:
            server.starttls()
            server.ehlo()
        server.login(sender, app_password)
        for i, to in enumerate(targets):
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = to
            msg["Message-ID"] = make_msgid()
            msg["Date"] = formatdate(localtime=True)
            try:
                server.sendmail(sender, [to], msg.as_string())
                results[to] = "sent"
            except smtplib.SMTPRecipientsRefused:
                # Rejected at send time -> definitely bad (an instant bounce).
                results[to] = "rejected"
            except Exception as exc:
                results[to] = f"error: {type(exc).__name__}: {exc}"
            if on_progress:
                on_progress(i + 1, len(targets))
            time.sleep(delay)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return results


def _build_msg(sender, to, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Message-ID"] = make_msgid()
    msg["Date"] = formatdate(localtime=True)
    return msg


def _open_smtp(s):
    """Open + authenticate one sender account. `s` is a dict with keys
    email, app_password, smtp_host, smtp_port, use_ssl."""
    if s.get("use_ssl"):
        srv = smtplib.SMTP_SSL(s["smtp_host"], s["smtp_port"], timeout=30)
        srv.ehlo()
    else:
        srv = smtplib.SMTP(s["smtp_host"], s["smtp_port"], timeout=30)
        srv.ehlo()
        srv.starttls()
        srv.ehlo()
    srv.login(s["email"], s["app_password"])
    return srv


def send_probes_multi(senders, targets, subject, body,
                      delay=2.0, limit=None, on_progress=None):
    """Round-robin `targets` across multiple sender accounts to spread volume.

    `senders` is a list of dicts (email, app_password, smtp_host, smtp_port,
    use_ssl). Returns (results, login_errors) where results maps
    email -> {'status': 'sent'|'rejected'|'error: ...', 'via': sender_email}.
    """
    targets = list(dict.fromkeys(a.strip() for a in targets if a and "@" in a))
    if limit:
        targets = targets[:limit]

    live = []            # [[sender_dict, connection], ...] — only accounts that logged in
    login_errors = {}
    for s in senders:
        try:
            live.append([s, _open_smtp(s)])
        except Exception as exc:
            login_errors[s.get("email", "?")] = f"{type(exc).__name__}: {exc}"

    results = {}
    if not live:
        return results, login_errors

    try:
        for i, to in enumerate(targets):
            slot = live[i % len(live)]
            s = slot[0]
            try:
                slot[1].sendmail(s["email"], [to], _build_msg(s["email"], to, subject, body).as_string())
                results[to] = {"status": "sent", "via": s["email"]}
            except smtplib.SMTPRecipientsRefused:
                results[to] = {"status": "rejected", "via": s["email"]}
            except Exception:
                # One reconnect attempt (dropped connection / timeout), then give up.
                try:
                    slot[1] = _open_smtp(s)
                    slot[1].sendmail(s["email"], [to], _build_msg(s["email"], to, subject, body).as_string())
                    results[to] = {"status": "sent", "via": s["email"]}
                except Exception as exc2:
                    results[to] = {"status": f"error: {type(exc2).__name__}: {exc2}",
                                   "via": s["email"]}
            if on_progress:
                on_progress(i + 1, len(targets))
            time.sleep(delay)
    finally:
        for _, srv in live:
            try:
                srv.quit()
            except Exception:
                pass
    return results, login_errors


def read_bounces_multi(senders, sent_addresses, scan_last=800):
    """Scan every account's inbox for bounces (sequential). Returns (bounced, errors)."""
    bounced = set()
    errors = {}
    for s in senders:
        try:
            bounced |= read_bounces(s["imap_host"], s["email"],
                                    s["app_password"], sent_addresses, scan_last)
        except Exception as exc:
            errors[s.get("email", "?")] = f"{type(exc).__name__}: {exc}"
    return bounced, errors


# ======================================================================
# Scaled sending: company-grouped, randomized-per-account, fully parallel.
# ======================================================================
def _group_key(item):
    """One key per company: prefer the Company name, else the email domain, so
    every address of a company lands in the same bucket (one sender account)."""
    comp = str(item.get("company", "") or "").strip().lower()
    if comp:
        return "c:" + comp
    dom = str(item.get("domain", "") or "").strip().lower()
    if not dom:
        e = str(item.get("email", ""))
        dom = e.split("@", 1)[1].lower() if "@" in e else ""
    return "d:" + dom


def plan_assignments(items, n_accounts, cap_lo=200, cap_hi=490, seed=None):
    """Distribute `items` (dicts with email + company/domain) across `n_accounts`.

    Rules:
      * every address of one company goes to ONE account (company kept intact),
      * each account gets a RANDOM cap in [cap_lo, cap_hi] (< 500 Gmail limit),
      * companies are placed in randomized order, first account that still has
        room takes the whole company.
    Returns (assignments, caps, leftover) where assignments[i] is the email list
    for account i, and leftover holds addresses that didn't fit under any cap.
    """
    rng = random.Random(seed)
    groups = {}
    for it in items:
        em = str(it.get("email", "")).strip()
        if em and "@" in em:
            groups.setdefault(_group_key(it), []).append(em)
    # de-dupe within each company, then randomize company order
    glist = [list(dict.fromkeys(v)) for v in groups.values()]
    rng.shuffle(glist)

    caps = [rng.randint(cap_lo, cap_hi) for _ in range(n_accounts)]
    assign = [[] for _ in range(n_accounts)]
    leftover = []
    for emails in glist:
        idx = next((i for i in range(n_accounts)
                    if len(assign[i]) + len(emails) <= caps[i]), None)
        if idx is None:
            leftover.extend(emails)
        else:
            assign[idx].extend(emails)
    return assign, caps, leftover


def _send_account_batch(sender, emails, subject, body, delay, results, lock, counter):
    """Worker: one account sends its whole batch. Lazy-connects (no upfront lag),
    reconnects once on a dropped/idle connection."""
    if not emails:
        return
    srv = None
    for to in emails:
        status = None
        for attempt in (1, 2):
            try:
                if srv is None:
                    srv = _open_smtp(sender)
                srv.sendmail(sender["email"], [to],
                             _build_msg(sender["email"], to, subject, body).as_string())
                status = "sent"
                break
            except smtplib.SMTPRecipientsRefused:
                status = "rejected"
                break
            except Exception as exc:
                srv = None                       # force reconnect on retry
                if attempt == 2:
                    status = f"error: {type(exc).__name__}: {exc}"
        with lock:
            results[to] = {"status": status, "via": sender["email"]}
            counter[0] += 1
        time.sleep(delay)
    try:
        srv.quit()
    except Exception:
        pass


def send_parallel(senders, assignments, subject, body, delay=1.0, counter=None):
    """Send every account's batch AT THE SAME TIME (one thread per account).

    `assignments[i]` are the emails for `senders[i]`. `counter` is an optional
    1-element list the caller can read from another thread for live progress.
    Returns results {email: {'status':..., 'via':...}}.
    """
    results = {}
    lock = threading.Lock()
    if counter is None:
        counter = [0]
    with ThreadPoolExecutor(max_workers=max(1, len(senders))) as ex:
        futures = [
            ex.submit(_send_account_batch, s, emails, subject, body, delay,
                      results, lock, counter)
            for s, emails in zip(senders, assignments)
        ]
        for f in futures:
            f.result()
    return results


def read_bounces_parallel(senders, sent_addresses, scan_last=500):
    """Scan every inbox for bounces CONCURRENTLY. Returns (bounced_set, errors)."""
    bounced = set()
    errors = {}
    lock = threading.Lock()

    def one(s):
        try:
            b = read_bounces(s["imap_host"], s["email"], s["app_password"],
                             sent_addresses, scan_last)
            with lock:
                bounced.update(b)
        except Exception as exc:
            with lock:
                errors[s.get("email", "?")] = f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=min(max(1, len(senders)), 12)) as ex:
        list(ex.map(one, senders))
    return bounced, errors


def _is_bounce(msg):
    # get() can return an email.header.Header (encoded headers), not a str, so
    # coerce before .lower() — otherwise "'Header' object has no attribute 'lower'".
    frm = str(msg.get("From") or "").lower()
    subj = str(msg.get("Subject") or "").lower()
    if any(b in frm for b in _BOUNCE_FROM):
        return True
    if any(b in subj for b in _BOUNCE_SUBJ):
        return True
    ctype = (msg.get_content_type() or "").lower()
    return ctype == "multipart/report"


def read_bounces(imap_host, sender, app_password, sent_addresses, scan_last=800):
    """Scan the inbox for bounce/NDR messages; return the set of `sent_addresses`
    that bounced. Matches by finding the recipient address inside the NDR."""
    sent = {a.strip().lower() for a in sent_addresses if a}
    bounced = set()
    box = imaplib.IMAP4_SSL(imap_host)
    try:
        box.login(sender, app_password)
        box.select("INBOX")
        typ, data = box.search(None, "ALL")
        ids = data[0].split()
        for num in ids[-scan_last:]:
            typ, md = box.fetch(num, "(RFC822)")
            if not md or not md[0]:
                continue
            msg = emaillib.message_from_bytes(md[0][1])
            if not _is_bounce(msg):
                continue
            text = ""
            for part in msg.walk():
                ct = part.get_content_type()
                if ct in ("text/plain", "message/delivery-status",
                          "text/rfc822-headers", "message/rfc822"):
                    try:
                        text += part.get_payload(decode=True).decode("utf-8", "replace")
                    except Exception:
                        pass
            for addr in EMAIL_RE.findall(text):
                a = addr.lower()
                if a in sent:
                    bounced.add(a)
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return bounced
