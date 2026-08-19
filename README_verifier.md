# 7-Layer Email Verification System

Consumes the **Verification Queue** CSV from the Streamlit app and validates
each candidate address through seven layers, rotating across a pool of **5 IPs
and 10 sender identities**.

## The 7 layers

| # | Layer | Network? | What it decides |
|---|-------|----------|-----------------|
| 1 | **Syntax / normalization** | no | Local part + domain are RFC-valid |
| 2 | **Domain + MX resolution** | DNS (cached) | Domain exists and accepts mail (MX, or A-record fallback) |
| 3 | **Disposable / role filter** | no | Rejects throwaway domains; flags `info@`, `sales@`, etc. |
| 4 | **Catch-all detection** | SMTP | Probes a random mailbox — does the domain accept *everything*? (cached per domain) |
| 5 | **SMTP RCPT handshake** | SMTP | The real check: `RCPT TO` for the actual address |
| 6 | **Greylist-aware retry** | SMTP | Re-runs layer 5 on tempfail (4xx) with backoff + a fresh IP |
| 7 | **Confidence scoring** | no | Fuses all signals → verdict + 0–100 confidence |

Layers 4 and 5 share **one** SMTP conversation per address (one connect, one
`MAIL FROM`, catch-all probe + real `RCPT` back to back), so a probe is a single
round trip, not two.

## Verdicts

| Verdict | Meaning |
|---------|---------|
| `deliverable` | MX accepted the RCPT on a **non-catch-all** domain (conf 90, or 97 if it matches a known address) |
| `risky` | Accepted, but it's a **catch-all** domain or a **role** account — acceptance doesn't prove the mailbox is real |
| `undeliverable` | Failed a hard layer (bad syntax, dead domain, disposable, or 550 reject) |
| `unknown` | Couldn't finish — tempfail survived all retries, MX unreachable, or an offline/dry run |

`unknown` is deliberately **not** the same as `undeliverable`: a temporary
failure is never treated as a false negative.

## Running it (on the VPS)

> **Port 25 outbound must be open.** Most home ISPs and many clouds (AWS, GCP,
> Azure, DigitalOcean by default) block it. If `telnet gmail-smtp-in.l.google.com 25`
> hangs from your box, verification cannot work there — that's an infra step, not code.

```bash
pip install -r requirements.txt          # adds dnspython
cp verifier_config.example.json verifier_config.json
# edit verifier_config.json: real IPs, identities, and EHLO hostnames

# offline sanity check first — layers 1-3 only, opens no sockets:
python verify_emails.py --input email_verification_queue.csv \
    --config verifier_config.json --output results.csv --dry-run

# full run:
python verify_emails.py --input email_verification_queue.csv \
    --config verifier_config.json --output results.csv
```

The run is **resumable**: state lives in `verification_state.db` (SQLite). Kill
it (Ctrl-C) and re-run the same command — finished addresses are skipped and
pending retries survive the restart.

## Configuration that actually matters

- **`source_ips`** — your 5 IPs. Each probe binds its outbound socket to the
  leased IP, so rotation is real, not cosmetic. Without this, every probe leaves
  from one interface and the pool is pointless.
- **`ehlo_by_ip`** — the EHLO hostname per IP. Each **must** have a valid A
  record *and* a matching PTR (reverse DNS). Mismatched forward/reverse DNS is
  the #1 reason serious MX servers (Google, Outlook, Mimecast) reject the
  conversation before you get an answer.
- **`sender_identities`** — the 10 `MAIL FROM` addresses. Use a domain you
  control with SPF set up.
- **Rate limits** — one live socket per domain, ≥6s between probes to the same
  domain (25s for the big/strict providers), plus hourly caps per domain and per
  IP. These are tuned to stay under the radar; loosen them at your own risk of
  getting IPs blocked.
- **`stop_on_first_hit`** — once one of a person's 5–7 candidates verifies
  `deliverable`, the rest are skipped. Big cost saver.

## Output columns

`row_id, person_id, name, candidate_email, domain, pattern, rank, is_known,
verdict, confidence, rcpt_code, catch_all, attempts, last_ip, status, reasons`

To get one best email per person: filter to `verdict = deliverable`, then keep
the lowest `rank` (or highest `confidence`) per `row_id`.

## Running it across several VPSs (shared queue + failover)

A single VPS with 1 IP tops out at ~150 checks/hour (~2.5/min). To run **5 VPSs as
one job** — no address verified twice, all nodes busy, and a dead node's work
auto-reclaimed — use the **coordinator**:

- `coordinator.py` runs on one box and owns a shared SQLite queue. It leases each
  *domain* to a single VPS at a time (so per-domain rate limits stay correct) and
  reclaims work from any node that stops heartbeating.
- Each VPS runs `verify_emails.py --coordinator http://<coord>:8900 --token ... --node-id vpsN`
  with its own single IP in `verifier_config.json`.

Throughput scales with IPs, not coordination: 5 IPs ≈ 750/hour ≈ 12.5/min. See
**deploy/COORDINATOR_SETUP.md** for the full walkthrough (WireGuard, tokens, the
Streamlit "Step 3: Verify Emails" integration). Uses only the standard library —
no extra dependency, no external database.

## What this is not

- It does **not** send email. It stops at `RCPT TO` and never issues `DATA`.
- Catch-all domains and role accounts are marked `risky`, never `deliverable` —
  no SMTP technique can confirm a specific mailbox on a catch-all server.
- Greylisting is handled by retry, but some servers greylist for 15+ minutes;
  those land as `unknown` if they don't clear within the retry window.
