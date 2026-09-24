# Email Verification — Excel → Both Pools Process

End-to-end runbook: from an Excel/leadership file to a verified, bounce-checked,
client-ready contact list, using the two VPS verification pools (Task 1 + Task 2)
and the Bounce Check tool.

---

## 0. The system at a glance

- **Two independent VPS pools**, each = 1 coordinator + 5 workers.
  - **Task 1** coordinator: `http://187.127.179.168:8900`
  - **Task 2** coordinator: `http://187.53.136.31:8900`
  - Tokens live in **Streamlit Secrets** (`coordinator_url`/`coordinator_token`,
    `coordinator2_url`/`coordinator2_token`) — never hard-code them.
- **SMTP verification** (no email is sent) → gives each address a verdict:
  - `deliverable` = real mailbox, high confidence
  - `risky` = accepted but the domain is **catch-all** (accepts anything) — usable but weaker
  - `undeliverable` = confirmed bad
  - `unknown` = server timed out / greylisted / tarpitted (no clear answer)
- **Bounce Check** = a *separate* step that ACTUALLY SENDS email and reads the
  bounce-backs to ground-truth the addresses.

**Golden rule:** one company's addresses always stay on **one pool** (split by
domain), and for sending, one company always goes through **one mailbox**.

---

## 1. Prepare the input Excel

The file should have **one row per company** with:

- **Company** (or `Company Name`)
- **Domain** — the company's email domain (e.g. `infosys.com`)
- **Role columns** in pairs — a name column + an EMAIL column, e.g.:
  `CEO` / `CEO EMAIL`, `Founder` / `Founder EMAIL`, `MD` / `MD EMAIL`,
  `CHRO` / `CHRO EMAIL`, `CTO` / `CTO EMAIL`, `Marketing Head` / `... EMAIL`, etc.

**A company is usable only if it has a Domain AND at least one named person.**
Companies missing either are dropped into a separate *no-data* list.

Rules applied during processing:
- **Dedupe people** — a person appears once per company (highest-priority role wins).
- **Drop role mailboxes** — `sales@`, `info@`, `contact@`, `hr@`, etc. are removed
  (we want real people).
- **Multi-founder cells** ("A, B & C") are split into separate people; org-name
  entries ("General Electric (GE)") are filtered out.
- **Known personal email present** → verify it directly.
- **No email but domain known** → generate candidate emails (default **4 per person**).

---

## 2A. Run it via the app — "Excel (both pools)" tab (simplest)

1. Open the Streamlit app → **📤 Excel (both pools)** tab.
2. **Upload** the `.xlsx` → click **Process file**.
   - It shows the split: ① Known emails · ② Generate (domain known) · ③ Need domain.
3. Click the phase buttons (they seed **both pools**, split by domain):
   - **① Send known emails to both pools** — verifies the emails already in the file.
   - **② Generate + send** — makes ~4 guesses per person and verifies them.
   - **③ (optional) DeepSeek find domains + generate + send** — looks up missing
     domains (needs `deepseek_api_key` in Secrets), then generates + verifies.
4. Click **🔄 Refresh Results** to watch progress (deliverable / undeliverable /
   unknown counts).
5. Download **Verified Contacts** when done.

---

## 2B. Run it via scripts (what we do for custom/large files)

Used when the file has non-standard columns or is large. Working dir:
`scraper_-main (1)/scraper_-main`.

**Step 1 — process the Excel into a people list** (dedupe, drop role emails, keep
companies with domain + a person). Produces `*_people_list.csv` and a
`*_no_data_companies.csv`, and pickles the people DataFrame `r`.

**Step 2 — build candidate queues and split by domain across pools:**
```python
import excel_leads as el
known = el.known_queue(r)          # emails already in the file
gen   = el.generated_queue(r, 4)   # 4 generated guesses per person
allc  = known + gen
# split so each DOMAIN stays on one pool
p0 = [c for c in allc if hash(str(c['domain'])) % 2 == 0]  # -> Task 1
p1 = [c for c in allc if hash(str(c['domain'])) % 2 == 1]  # -> Task 2
```

**Step 3 — seed both coordinators** (POST `/seed`, `clear:true` on the FIRST chunk
of each pool to wipe the previous completed batch — always export/save it first):
```
POST {coordinator}/seed
  headers: Authorization: Bearer {token}
  body:    {"clear": <true for first chunk>, "candidates": [ ...up to ~1500... ]}
```
Each candidate = `{candidate_email, row_id, id, name, domain, pattern, rank, is_known_email}`.

**Step 4 — confirm workers picked it up** (`GET /status`): `pending` should be
draining, `claimed` = 12 per pool, and the `leases` list shows active worker nodes.

---

## 3. Monitor verification

Poll both coordinators' `GET /counts` and combine:
- `verdict`: deliverable / risky / undeliverable / unknown
- `status`: pending + retry = **queue remaining**; claimed = in-flight; done

**Progress %** = (total_seeded − (pending + retry)) / total_seeded.

Notes:
- **Cold/new domains start slow** (greylisting) and speed up over hours as they warm.
- A batch over already-warmed domains runs much faster.
- Ask for **"status"** anytime for a live pull.

---

## 4. Build the verified list

When both queues hit 0:
1. `GET /export` from **both** coordinators (full per-address results).
2. Keep rows with verdict `deliverable` or `risky`.
3. Map each back to its person via `row_id` → the processed people DataFrame
   (gives exact **Company / Designation / Name**).
4. Dedupe (one best email per person if wanted) → save **`*_Verified.xlsx` + `.csv`**
   with columns **Company · Name · Designation · Email · Domain · Verdict**.

`deliverable` = safest list. `risky` = usable but bounce-prone (catch-all).

---

## 5. (Optional) Re-run the unknowns — recovers more

The `unknown` bucket often converts on a second pass as greylisting clears / IPs warm.

1. From the export, collect all `unknown` candidate emails, per pool.
2. **Re-queue in place** (non-destructive — keeps existing verdicts): for each
   unknown, `POST /mark_retry` with `{"email", "attempts":0, "next_attempt_at": now}`.
3. Workers automatically re-probe them.
4. When done, rebuild the verified list — the new verifies = current verified minus
   the pre-re-run count.

*(Typical recovery: a few hundred to a few thousand extra verified, depending on
batch size.)*

---

## 6. Bounce Check (send real email + detect bounces)

Turns `risky/unknown` guesses into ground truth, and produces the final client list.

**Prep the upload file** — a CSV with `Email` (+ optional `Company`, `Domain`,
`Name`, `Designation`). Company/Domain drive grouping; Name/Designation flow into
the exported no-bounce sheet.

**In the 📧 Bounce Check tab:**
1. **📊 Check usage now** — reads each mailbox's *Sent folder* to show how many it
   sent today and auto-selects only accounts **with quota** (Gmail caps ~200–260/day
   for fresh accounts; set **Max per account = ~200**).
2. Upload the CSV → review the **Send plan** (shows accounts needed + per-account
   split, one company per account).
3. **Send in parallel** — all selected accounts send at once; a **Send log** records
   which account sent which email (download it).
4. Later, **🔍 Check bounces now** — scans every inbox (in parallel) and reports:
   - ❌ Bounced (invalid) · ✅ No bounce (likely valid) · **Inboxes read: X/N**
   - Downloads: **No-bounce list** / **Bounced list** / **Full results**
     (no-bounce/bounced are deduped **one per person**, client-ready).

**Read the results correctly:**
- Metrics are **per email**; the download lists are **per person** (deduped) — so
  the numbers differ, and that's expected.
- **"Inboxes read: X/N"** — if any inbox failed, bounces landing there are NOT
  counted; fix those accounts (IMAP on / correct app password) and re-check.
- Bounces arrive over **minutes-to-hours** — re-check later to catch late ones.
- **Catch-all** domains never bounce, so "no bounce" ≠ guaranteed valid there.

---

## 7. Sending limits — the #1 gotcha

- Gmail cuts an account off at its **daily limit** (~200–260 fresh). Over-shooting
  gives `SMTPServerDisconnected` / `550 5.4.5 Daily user sending limit exceeded`.
- **Always run "Check usage now" first** so you only send from accounts with quota.
- Set **Max per account ≈ 200**. Spread big lists across more mailboxes / more days.
- Anything that errored **did not send** — pull it from the Send log
  (`Status = error`) and resend tomorrow / from fresh accounts.

---

## 8. Final client deliverable

The **No-bounce list** download is already client-ready:
**Company · Name · Designation · Email**, one row per person, no duplicates.
Merge multiple no-bounce files if needed, then format as a polished `.xlsx`
(title banner, frozen bold header, auto-filter, alternating rows) for the client.

---

## Quick reference — one full cycle

1. Prepare Excel (Company + Domain + role name/email columns).
2. Process → dedupe, drop role emails, generate 4/person.
3. Seed both pools (split by domain; `clear:true` first).
4. Monitor `/counts` until queue = 0.
5. Export → build **Verified list** (deliverable + risky).
6. (Optional) Re-queue `unknown` → rebuild verified list.
7. Bounce Check: usage check → send (cap ~200) → check bounces.
8. Download **No-bounce list** → format → send to client.
