# Developer Handoff — Apollo Email Verifier & Bounce Checker

Complete context for a developer picking this project up. It covers the
architecture, every module, how to run/deploy, and the operational workflows.
For the day-to-day operator runbook see **`PROCESS_Excel_Both_Pools.md`**.

> ⚠️ **Secrets are NOT in this repo and must never be committed.** All tokens,
> Gmail app passwords, and API keys live in **Streamlit Secrets** (for the app)
> and in per-VPS config files created on the servers. If any secret was ever
> exposed, rotate it (regenerate app passwords, coordinator tokens, DeepSeek key).

---

## 1. What this system does

Two capabilities:

1. **SMTP verification (no email sent)** — takes people + company domains,
   generates candidate email addresses, and probes each mailbox over SMTP (RCPT,
   never DATA). Each address gets a verdict: `deliverable`, `risky` (catch-all
   accepted), `undeliverable`, or `unknown` (timeout/greylist).
2. **Bounce checking (sends real email)** — sends to addresses and reads the
   bounce-backs (NDRs) from the sender's own inbox via IMAP, to ground-truth them.

Runs across **two independent VPS pools** (Task 1, Task 2), each a coordinator +
5 workers, so companies are split across pools and verified in parallel.

---

## 2. Architecture

```
                Streamlit app (app.py)  ── UI: Task1 / Task2 / Excel / Bounce Check
                        │
       HTTP (token)     │  seed / status / export / mark_retry
                        ▼
   ┌─────────────── Coordinator (coordinator.py) ───────────────┐   × 2 pools
   │  ThreadingHTTPServer + SQLite (leased_store.py / state.py)  │
   │  domain-leasing queue with failover                        │
   └───────────────┬───────────────────────────┬───────────────┘
                   │ /claim  /mark_done ...      │
        ┌──────────▼─────────┐        ┌──────────▼─────────┐
        │ Worker (runner.py) │  × 5   │ Worker (runner.py) │
        │ SMTPProbe(layers)  │        │ RemoteStore client │
        └────────────────────┘        └────────────────────┘
```

- **Coordinator** owns the queue. Workers **lease a domain**, probe its
  candidates (rate-limited, stop-on-first-hit per person), and report verdicts.
  If a worker dies, its lease expires and another worker picks the domain up
  (failover). SQLite only (no Postgres).
- The **app** never verifies; it seeds candidates into the coordinators and reads
  status/exports back over HTTP.

**Pools (infra):**
- Task 1 coordinator: `http://187.127.179.168:8900` (workers vps1–vps5)
- Task 2 coordinator: `http://187.53.136.31:8900` (workers t2vps1–t2vps5)
- Tokens: in Streamlit Secrets (`coordinator_token`, `coordinator2_token`).

---

## 3. Repo map

| Path | What it is |
|---|---|
| `app.py` | Streamlit UI. Tabs: Task 1, Task 2, Excel (both pools), Bounce Check. Talks to coordinators over HTTP; reads secrets. |
| `coordinator.py` | HTTP coordinator (ThreadingHTTPServer). Endpoints below. |
| `verify_emails.py` | Worker entrypoint — runs a `verifier.runner.Verifier` against a coordinator. |
| `verifier/runner.py` | Worker loop: claim → probe → mark. `keep_alive` idle-polls for UI-pushed work. |
| `verifier/layers.py` | `SMTPProbe` — the actual SMTP conversation (EHLO/MAIL/RCPT), STARTTLS handling, source-IP binding. |
| `verifier/leased_store.py` | Coordinator-side SQLite store with domain leasing + failover. |
| `verifier/state.py` | Base SQLite store (candidates table, statuses). Journal mode = DELETE (WAL unsupported on some VPS FS). |
| `verifier/remote_store.py` | HTTP client the worker uses to talk to the coordinator (same interface as the store). |
| `verifier/pool.py`, `dnsx.py`, `config.py` | connection pool, DNS/MX resolution, config dataclass. |
| `email_patterns.py` | Candidate-email generator (naming conventions, free-mail + placeholder filtering). |
| `excel_leads.py` | Excel → deduped people; role-email filter; company-pattern detection; `known_queue`/`generated_queue`; DeepSeek domain lookup. |
| `bounce_check.py` | Bounce Check engine: parallel SMTP send (company-grouped, randomized <500/acct), IMAP bounce reading (batched fetch), per-account daily-usage from Sent folder. |
| `verifier_config.*.json` | Per-VPS config (source IPs, EHLO host, sender identity, rate limits, timeouts). `.vps1-5` = Task 1, `.t2vps1-5` = Task 2. |
| `deploy/` | VPS + systemd setup scripts and setup guides. |
| `requirements.txt` | streamlit, pandas, dnspython, openpyxl. |

---

## 4. Coordinator HTTP API (auth: `Authorization: Bearer <token>`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/seed` | Add candidates. `{"clear": bool, "candidates":[...]}`. `clear:true` wipes prior batch. |
| POST | `/claim` | Worker claims N candidates for a node (leases a domain). |
| POST | `/mark_done` `/mark_retry` `/mark_error` | Report a result / defer / error. |
| POST | `/reset_stale_claims` `/heartbeat` | Failover housekeeping. |
| GET | `/counts` | verdict + status tallies (drives monitoring). |
| GET | `/status` | counts + active leases (which workers are busy). |
| GET | `/export` | Full per-address results (for building verified lists). |
| GET | `/health` | Liveness. |

Candidate shape for `/seed`:
`{candidate_email, row_id, id, name, domain, pattern, rank, is_known_email}`.

---

## 5. Running locally

```bash
python -m venv venv && ./venv/Scripts/pip install -r requirements.txt
# App (needs Streamlit Secrets with coordinator_url/token + coordinator2_*):
./venv/Scripts/streamlit run app.py
```

The app is deployed on **Streamlit Cloud** (auto-deploys on push to `main`).
Secrets are set in the Streamlit Cloud dashboard, not in the repo.

**Streamlit Secrets keys:**
```toml
coordinator_url = "http://187.127.179.168:8900"
coordinator_token = "..."
coordinator2_url = "http://187.53.136.31:8900"
coordinator2_token = "..."
deepseek_api_key = "..."          # optional, for domain lookup

# Bounce Check senders (one or many):
[[senders]]
email = "acct1@gmail.com"
app_password = "16-char-app-password"   # Gmail app password, IMAP must be ON
# ... repeat [[senders]] per mailbox
```

---

## 6. Deploying a VPS pool

On each VPS (see `deploy/VPS_SETUP.md`, `deploy/COORDINATOR_SETUP.md`):
```bash
git clone <repo> scraper && cd scraper
cp verifier_config.<name>.json verifier_config.json   # e.g. vps2 or t2vps3
# Coordinator machine (runs coordinator + a worker):
ROLE=coordinator NODE=vps2 bash deploy/install_systemd.sh
# Worker machine:
ROLE=worker NODE=vps3 COORD=http://<coord-ip>:8900 TOKEN=xxxx bash deploy/install_systemd.sh
```
`install_systemd.sh` creates the venv, writes `verifier-coordinator.service` /
`verifier-worker.service` (auto-start on boot, restart on crash), and starts them.
It uses `enable` + `restart` (NOT `enable --now`, which won't restart a running
service — a bug that once caused code fixes to silently not load).

Logs: `journalctl -u verifier-coordinator -f` (or `verifier-worker`).

---

## 7. Operational workflows (summary — full runbook in PROCESS_Excel_Both_Pools.md)

- **Verify from an Excel:** process → generate 4 emails/person → seed both pools
  (split by domain) → monitor `/counts` → export → build verified list.
- **Re-run unknowns:** collect `unknown` emails → `POST /mark_retry` each → workers
  re-probe (non-destructive). Recovers more as greylisting clears.
- **Bounce check:** usage-check accounts → send (cap ~200/acct, company-grouped,
  parallel) → check bounces (IMAP) → download no-bounce (client-ready).

---

## 8. Known issues / gotchas (learned the hard way)

- **Gmail daily send limits (~200–260 fresh accounts).** Over-shooting →
  `SMTPServerDisconnected` / `550 5.4.5 Daily user sending limit exceeded`. Always
  run the **usage check** first and cap ~200. Errored sends never went out — resend
  from the Send log (`Status = error`).
- **Streamlit module hot-reload.** On deploy, `app.py` can reload while an imported
  module (`bounce_check`) stays cached → `AttributeError: module has no attribute
  ...`. Fix: `hasattr()` guards on new functions **and reboot the app** (Manage app →
  Reboot) to force a full reimport.
- **IMAP scan can hang / be slow.** Mitigations already in `bounce_check.read_bounces`:
  30s socket timeout, IMAP SEARCH for only bounce messages, and **batched FETCH**.
- **Catch-all domains** accept everything → show as `risky` (verify) and never bounce
  (bounce check). "No bounce" ≠ guaranteed valid there.
- **WAL journal mode** is unsupported on some VPS filesystems → store uses DELETE mode.
- **Cold/new domains** greylist heavily on first contact; throughput ramps over hours.

---

## 9. First tasks for a new developer

1. Read this doc + `PROCESS_Excel_Both_Pools.md`.
2. Get read access to Streamlit Secrets (or your own test coordinator + a throwaway
   Gmail with an app password) — **never** commit secrets.
3. Run the app locally against the coordinators; open each tab.
4. Trace one verification: `excel_leads.process_excel` → `generated_queue` →
   `/seed` → worker `runner.py`/`layers.py` → `/export`.
5. Trace one bounce check: `bounce_check.send_parallel` → `read_bounces_parallel`.
