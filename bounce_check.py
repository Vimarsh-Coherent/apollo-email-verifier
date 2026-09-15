"""Bounce checking: SEND a real email to each address and detect bounces by
reading the sender's own inbox over IMAP.

This ACTUALLY SENDS email (unlike verifier/, which stops at RCPT and never sends
DATA). High bounce volume can get the sending account suspended, so use a
DEDICATED account and vetted lists only. Credentials are read from Streamlit
Secrets by the caller and passed in here - nothing is stored or hardcoded.
"""

import email as emaillib
import imaplib
import re
import smtplib
import time
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


def _is_bounce(msg):
    frm = (msg.get("From") or "").lower()
    subj = (msg.get("Subject") or "").lower()
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
