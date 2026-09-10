import streamlit as st
import json
import urllib.request
import urllib.error
import pandas as pd
from io import StringIO

from email_patterns import EMAIL_PATTERNS, build_candidates
import excel_leads

st.set_page_config(
    page_title="Apollo Scraper - JSON to CSV Converter",
    page_icon="📊",
    layout="wide"
)

st.title("📊 Apollo Scraper")


# Helper function to extract people/contacts data from JSON (universal)
def extract_people_data(json_data):
    """Extract and flatten people/contacts data from JSON - handles both structures"""
    rows = []
    data_list = json_data.get("people", []) or json_data.get("contacts", [])

    for person in data_list:
        row = {
            "id": person.get("id", ""),
            "name": person.get("name", ""),
            "first_name": person.get("first_name", ""),
            "last_name": person.get("last_name", ""),
            "title": person.get("title", ""),
            "headline": person.get("headline", ""),
            "linkedin_url": person.get("linkedin_url", ""),
            "city": person.get("city", ""),
            "state": person.get("state", ""),
            "country": person.get("country", ""),
            "postal_code": person.get("postal_code", ""),
            "formatted_address": person.get("formatted_address", ""),
            "time_zone": person.get("time_zone", ""),
            "seniority": person.get("seniority", ""),
            "organization_id": person.get("organization_id", ""),
            "organization_name": person.get("organization_name", ""),
        }

        primary_email = person.get("email", "")
        email_status = person.get("email_status", "")
        email_true_status = person.get("email_true_status", "")

        contact_emails = person.get("contact_emails", [])
        if contact_emails and not primary_email:
            primary_email = contact_emails[0].get("email", "")
            if not email_status:
                email_status = contact_emails[0].get("email_status", "")
            if not email_true_status:
                email_true_status = contact_emails[0].get("email_true_status", "")

        all_emails = [primary_email] if primary_email else []
        if contact_emails:
            all_emails.extend([e.get("email", "") for e in contact_emails
                               if e.get("email") and e.get("email") != primary_email])
        all_emails = [e for e in all_emails if e]

        row["email"] = primary_email
        row["email_status"] = email_status
        row["email_true_status"] = email_true_status
        row["all_emails"] = ", ".join(all_emails) if all_emails else ""

        phone_numbers = person.get("phone_numbers", [])
        if phone_numbers:
            primary_phone = phone_numbers[0].get("raw_number", "") or phone_numbers[0].get("sanitized_number", "")
            all_phones = [p.get("raw_number", "") or p.get("sanitized_number", "")
                          for p in phone_numbers if p.get("raw_number") or p.get("sanitized_number")]
            all_phones = [p for p in all_phones if p]
            row["phone"] = primary_phone
            row["all_phones"] = ", ".join(all_phones) if all_phones else ""
        else:
            row["phone"] = person.get("phone", "") or person.get("sanitized_phone", "")
            row["all_phones"] = row["phone"]

        org = person.get("organization", {})
        if org:
            row["org_name"] = org.get("name", "") or person.get("organization_name", "")
            row["org_website"] = org.get("website_url", "")
            row["org_linkedin"] = org.get("linkedin_url", "")
            row["org_employees"] = org.get("estimated_num_employees", "")
            row["org_industries"] = ", ".join(org.get("industries", [])) if isinstance(org.get("industries"), list) else ""
            row["org_keywords"] = ", ".join(org.get("keywords", [])) if isinstance(org.get("keywords"), list) else ""
            row["org_phone"] = org.get("phone", "") or org.get("sanitized_phone", "")
            row["org_founded_year"] = org.get("founded_year", "")
        else:
            row["org_name"] = person.get("organization_name", "")
            row["org_website"] = ""
            row["org_linkedin"] = ""
            row["org_employees"] = ""
            row["org_industries"] = ""
            row["org_keywords"] = ""
            row["org_phone"] = ""
            row["org_founded_year"] = ""

        row["twitter_url"] = person.get("twitter_url", "")
        row["facebook_url"] = person.get("facebook_url", "")
        row["person_id"] = person.get("person_id", "")
        row["account_id"] = person.get("account_id", "")
        row["created_at"] = person.get("created_at", "")
        row["updated_at"] = person.get("updated_at", "")

        rows.append(row)

    return rows


def to_csv_bytes(df):
    """UTF-8 with BOM so Excel renders accented names correctly."""
    buffer = StringIO()
    df.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8-sig")


# ----------------------------------------------------------------------
# Coordinator client - talks to the VPS verification pool over HTTP.
# ----------------------------------------------------------------------
COORD_TIMEOUT = 30


def coord_request(base_url, token, method, path, payload=None):
    """One HTTP call to the coordinator. Raises on failure with a readable msg."""
    url = base_url.rstrip("/") + path
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=COORD_TIMEOUT) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"Cannot reach coordinator at {url}: {exc}") from exc


def _secret(key):
    try:
        return st.secrets.get(key, "")
    except Exception:
        return ""


# ----------------------------------------------------------------------
# Multi-pool helpers (Excel bulk mode uses BOTH Task 1 + Task 2 pools).
# ----------------------------------------------------------------------
def _pools():
    """Return the configured coordinator pools: [(label, url, token), ...]."""
    pools = []
    if _secret("coordinator_url") and _secret("coordinator_token"):
        pools.append(("Task 1", _secret("coordinator_url"), _secret("coordinator_token")))
    if _secret("coordinator2_url") and _secret("coordinator2_token"):
        pools.append(("Task 2", _secret("coordinator2_url"), _secret("coordinator2_token")))
    return pools


def _seed_split(candidates, pools, clear):
    """Split candidates across pools by domain (each domain -> one pool, so
    per-domain rate limiting stays correct) and seed each pool."""
    import hashlib
    groups = [[] for _ in pools]
    for c in candidates:
        h = int(hashlib.md5((c.get("domain") or "").encode()).hexdigest(), 16)
        groups[h % len(pools)].append(c)
    total = 0
    for (label, url, token), group in zip(pools, groups):
        for i in range(0, len(group), 1000):
            part = group[i:i + 1000]
            coord_request(url, token, "POST", "/seed",
                          {"candidates": part, "clear": clear and i == 0})
            total += len(part)
    return total


def _combined_status(pools):
    """Merge status + export across all pools."""
    import collections
    sc, vc, rows = collections.Counter(), collections.Counter(), []
    for label, url, token in pools:
        try:
            status = coord_request(url, token, "GET", "/status")
            for k, v in status.get("counts", {}).get("status", {}).items():
                sc[k] += v
            for k, v in status.get("counts", {}).get("verdict", {}).items():
                vc[k] += v
            rows += coord_request(url, token, "GET", "/export").get("rows", [])
        except RuntimeError:
            pass
    return sc, vc, rows


# Columns that carry an email *status*/verdict - stripped from the final sheet.
_STATUS_COLS = {
    "email_status", "email_true_status", "verdict", "confidence", "status",
    "rcpt_code", "catch_all", "reasons", "attempts", "last_ip",
}


def build_apollo_output(contacts_df, res_df):
    """Final deliverable: the original Apollo rows, but only for people whose
    email verified deliverable or risky, with that verified email filled in and
    every status column removed."""
    if contacts_df is None or res_df is None or res_df.empty:
        return pd.DataFrame()

    d = res_df.copy()
    d = d[d.get("verdict", "").isin(["deliverable", "risky"])]
    if d.empty:
        return pd.DataFrame()

    order = {"deliverable": 0, "risky": 1}
    d["_v"] = d["verdict"].map(order).fillna(9)
    d["_conf"] = pd.to_numeric(d.get("confidence"), errors="coerce").fillna(0)
    d["_rank"] = pd.to_numeric(d.get("rank"), errors="coerce").fillna(999)
    d = d.sort_values(["row_id", "_v", "_conf", "_rank"],
                      ascending=[True, True, False, True])
    best = d.groupby("row_id", as_index=False).first()

    contacts = contacts_df.reset_index(drop=True)
    out_rows = []
    for _, hit in best.iterrows():
        try:
            idx = int(hit["row_id"])
        except (TypeError, ValueError, KeyError):
            continue
        if idx < 0 or idx >= len(contacts):
            continue
        person = contacts.iloc[idx].to_dict()
        person["email"] = hit["candidate_email"]
        out_rows.append(person)

    out = pd.DataFrame(out_rows)
    drop = [c for c in out.columns if c in _STATUS_COLS]
    if drop:
        out = out.drop(columns=drop)
    return out


# ======================================================================
# One self-contained task pipeline (pages -> convert -> generate -> verify).
# Called once per tab with its own namespace and its own VPS coordinator, so
# Task 1 and Task 2 run completely independently and in parallel.
# ======================================================================
def render_task(task, label, num_pages, url_key, token_key):
    st.markdown(
        f"Paste JSON data from each Apollo page below (up to **{num_pages} pages**). "
        "All data is combined into a single CSV."
    )

    page_tabs = st.tabs([f"Page {i + 1}" for i in range(num_pages)])
    json_inputs = {}
    for i, tab in enumerate(page_tabs):
        with tab:
            page_num = i + 1
            ji = st.text_area(
                f"Paste JSON data from Page {page_num}",
                height=400,
                placeholder=f"Paste your JSON data from page {page_num} here...",
                key=f"{task}_page_{page_num}",
            )
            json_inputs[page_num] = ji
            if ji:
                st.caption(f"📝 {len(ji)} characters")

    if st.button("🔄 Convert All Pages to CSV", type="primary",
                 use_container_width=True, key=f"{task}_convert"):
        all_rows, pages_processed, pages_with_errors = [], 0, []
        for page_num in range(1, num_pages + 1):
            ji = json_inputs.get(page_num, "")
            if ji.strip():
                try:
                    rows = extract_people_data(json.loads(ji))
                    if rows:
                        all_rows.extend(rows)
                        pages_processed += 1
                        st.success(f"✅ Page {page_num}: {len(rows)} records extracted")
                    else:
                        st.warning(f"⚠️ Page {page_num}: No people/contacts data found")
                except json.JSONDecodeError as e:
                    st.error(f"❌ Page {page_num}: Invalid JSON - {e}")
                    pages_with_errors.append(page_num)
                except Exception as e:
                    st.error(f"❌ Page {page_num}: Error - {e}")
                    pages_with_errors.append(page_num)

        if all_rows:
            st.session_state[f"{task}_contacts_df"] = pd.DataFrame(all_rows)
            st.session_state[f"{task}_pages_processed"] = pages_processed
            st.session_state[f"{task}_pages_with_errors"] = pages_with_errors
            for k in ("emails_wide", "emails_long", "emails_stats"):
                st.session_state.pop(f"{task}_{k}", None)
        else:
            st.session_state.pop(f"{task}_contacts_df", None)
            st.error("❌ No data found in any page. Paste JSON in at least one page.")

    df = st.session_state.get(f"{task}_contacts_df")
    if df is None:
        return

    pages_processed = st.session_state.get(f"{task}_pages_processed", 0)
    pages_with_errors = st.session_state.get(f"{task}_pages_with_errors", [])

    st.divider()
    st.success(f"🎉 Processed {pages_processed} page(s) with {len(df)} total records!")
    if pages_with_errors:
        st.warning(f"⚠️ {len(pages_with_errors)} page(s) had errors: "
                   f"{', '.join(map(str, pages_with_errors))}")

    st.subheader("📋 Data Preview")
    st.dataframe(df, use_container_width=True, height=400)
    st.download_button(
        f"📥 Download CSV ({len(df)} records)", data=to_csv_bytes(df),
        file_name=f"{task}_contacts_combined.csv", mime="text/csv",
        use_container_width=True, key=f"{task}_dl_csv",
    )

    st.subheader("📊 Statistics")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Records", len(df))
    c2.metric("Pages Processed", pages_processed)
    c3.metric("With Email", len(df[df["email"] != ""]))
    vcount = (len(df[df["email_status"].astype(str).str.lower() == "verified"])
              if "email_status" in df.columns else 0)
    c4.metric("Verified Emails", vcount)
    c5.metric("Unique Countries", df["country"].nunique())
    c6, c7, c8 = st.columns(3)
    c6.metric("Unique Companies", df["org_name"].nunique() if "org_name" in df.columns else 0)
    c7.metric("C-Suite", len(df[df["seniority"] == "c_suite"]))
    c8.metric("Vice Presidents", len(df[df["seniority"] == "vp"]))

    # ---- Step 2: generate email permutations ----
    st.divider()
    st.subheader("✉️ Step 2: Generate Email Permutations")
    st.markdown("Generate candidate addresses per person from name + company domain. "
                "The output feeds the SMTP verification pipeline.")

    epp = st.slider("Emails to generate per person", 2, 7, 5, key=f"{task}_epp")
    o1, o2 = st.columns(2)
    with o1:
        repro = st.checkbox("Reproducible pattern picks", value=True, key=f"{task}_repro")
    with o2:
        salt = st.number_input("Shuffle seed", 0, 9999, 0, 1, key=f"{task}_salt")

    if st.button("🎲 Generate Emails", type="primary",
                 use_container_width=True, key=f"{task}_gen"):
        wide, long, stats = build_candidates(df, epp, reproducible=repro, salt=int(salt))
        st.session_state[f"{task}_emails_wide"] = pd.DataFrame(wide)
        st.session_state[f"{task}_emails_long"] = pd.DataFrame(long)
        st.session_state[f"{task}_emails_stats"] = stats

    wide_df = st.session_state.get(f"{task}_emails_wide")
    long_df = st.session_state.get(f"{task}_emails_long")
    stats = st.session_state.get(f"{task}_emails_stats")

    if wide_df is not None and stats is not None:
        if stats["candidates_total"] == 0:
            st.error("❌ No candidates generated — no usable company domain found "
                     "(records need a corporate email or an `org_website`).")
        else:
            st.success(f"✅ Generated {stats['candidates_total']} candidate emails for "
                       f"{stats['people_with_domain']} people across "
                       f"{stats['unique_domains']} domains.")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Candidate Emails", stats["candidates_total"])
            m2.metric("People Covered", stats["people_with_domain"])
            m3.metric("Unique Domains", stats["unique_domains"])
            skipped = (stats["people_skipped_no_domain"]
                       + stats["people_skipped_free_email"]
                       + stats["people_skipped_no_name"])
            m4.metric("Skipped", skipped)

            tw, tl, tp = st.tabs(["📋 Per Person", "🔍 Verification Queue", "🧩 Pattern Usage"])
            with tw:
                st.dataframe(wide_df, use_container_width=True, height=400)
                st.download_button(
                    f"📥 Download Per-Person CSV ({len(wide_df)} rows)",
                    data=to_csv_bytes(wide_df),
                    file_name=f"{task}_emails_per_person.csv", mime="text/csv",
                    use_container_width=True, key=f"{task}_dl_wide",
                )
            with tl:
                st.caption("One row per candidate address — the file the SMTP workers consume.")
                st.dataframe(long_df, use_container_width=True, height=400)
                st.download_button(
                    f"📥 Download Verification Queue ({len(long_df)} rows)",
                    data=to_csv_bytes(long_df),
                    file_name=f"{task}_verification_queue.csv", mime="text/csv",
                    use_container_width=True, key=f"{task}_dl_long",
                )
            with tp:
                counts = (long_df["pattern"].value_counts()
                          .rename_axis("pattern").reset_index(name="times_used"))
                st.dataframe(counts, use_container_width=True, height=400)
                st.caption(f"{len(EMAIL_PATTERNS)} patterns in the catalogue.")

    # ---- Step 3: verify on this task's VPS pool ----
    long_df = st.session_state.get(f"{task}_emails_long")
    if long_df is None or len(long_df) == 0:
        return

    st.divider()
    st.subheader("🔎 Step 3: Verify Emails")
    st.markdown(f"Send the candidate queue to **{label}'s** VPS verification pool. One "
                "coordinator leases each domain to a single VPS, checks the mailboxes "
                "over SMTP, and reclaims work automatically if a VPS drops.")

    secret_url = _secret(url_key)
    secret_token = _secret(token_key)
    if secret_url and secret_token:
        coord_url, coord_token = secret_url, secret_token
        st.caption(f"🔗 Connected to {label}'s VPS verification pool.")
    else:
        cc1, cc2 = st.columns([2, 1])
        with cc1:
            coord_url = st.text_input(
                "Coordinator URL",
                value=st.session_state.get(f"{task}_coord_url", ""),
                placeholder="http://10.0.0.1:8900", key=f"{task}_url",
            )
        with cc2:
            coord_token = st.text_input(
                "Token", value=st.session_state.get(f"{task}_coord_token", ""),
                type="password", key=f"{task}_tok",
            )

    ready = bool(coord_url and coord_token)
    vb1, vb2 = st.columns(2)
    with vb1:
        start_clicked = st.button("🚀 Send to Verifier & Start", type="primary",
                                  use_container_width=True, disabled=not ready,
                                  key=f"{task}_start")
    with vb2:
        refresh_clicked = st.button("🔄 Refresh Results", use_container_width=True,
                                    disabled=not ready, key=f"{task}_refresh")

    if not ready:
        st.info(f"Set `{url_key}` and `{token_key}` in the app's **Secrets** to "
                f"connect {label}'s VPS pool.")

    if start_clicked:
        st.session_state[f"{task}_coord_url"] = coord_url
        st.session_state[f"{task}_coord_token"] = coord_token
        records = long_df.to_dict(orient="records")
        payload = [
            {
                "candidate_email": r.get("candidate_email", ""),
                "row_id": int(r["row_id"]) if str(r.get("row_id", "")).strip() != "" else None,
                "id": str(r.get("id", "")),
                "name": str(r.get("name", "")),
                "domain": str(r.get("domain", "")),
                "pattern": str(r.get("pattern", "")),
                "rank": int(r["rank"]) if str(r.get("rank", "")).strip() != "" else None,
                "is_known_email": bool(r.get("is_known_email", False)),
            }
            for r in records if r.get("candidate_email")
        ]
        try:
            chunk, total_added = 1000, 0
            prog = st.progress(0.0, text="Sending queue to coordinator...")
            for i in range(0, len(payload), chunk):
                part = payload[i:i + chunk]
                resp = coord_request(coord_url, coord_token, "POST", "/seed",
                                     {"candidates": part, "clear": i == 0})
                total_added += resp.get("added", 0)
                prog.progress(min(1.0, (i + len(part)) / len(payload)),
                              text=f"Sent {i + len(part)}/{len(payload)} addresses")
            prog.empty()
            st.session_state[f"{task}_verify_started"] = True
            st.success(f"✅ Queued {total_added} addresses. {label}'s VPS workers will "
                       "pick them up. Use **Refresh Results** to track progress.")
        except RuntimeError as exc:
            st.error(f"❌ {exc}")

    if (refresh_clicked or st.session_state.get(f"{task}_verify_started")) and ready:
        try:
            status = coord_request(coord_url, coord_token, "GET", "/status")
        except RuntimeError as exc:
            st.error(f"❌ {exc}")
            status = None

        if status is not None:
            by_status = status.get("counts", {}).get("status", {})
            by_verdict = status.get("counts", {}).get("verdict", {})
            total = sum(by_status.values())
            done = by_status.get("done", 0) + by_status.get("error", 0)

            st.progress((done / total) if total else 0.0,
                        text=f"{done}/{total} addresses checked")

            # Risky is treated as deliverable (usable) and folded into that count.
            deliverable_count = by_verdict.get("deliverable", 0) + by_verdict.get("risky", 0)
            sm1, sm2, sm3 = st.columns(3)
            sm1.metric("✅ Deliverable", deliverable_count)
            sm2.metric("❌ Undeliverable", by_verdict.get("undeliverable", 0))
            sm3.metric("❔ Unknown", by_verdict.get("unknown", 0))

            leases = status.get("leases", [])
            if leases:
                nodes = sorted({l["node"] for l in leases})
                st.caption(f"🖥️ Working now: **{', '.join(nodes)}** — "
                           f"{len(leases)} domain(s) leased · "
                           f"pending {by_status.get('pending', 0)} · "
                           f"in-flight {by_status.get('claimed', 0)} · "
                           f"retry {by_status.get('retry', 0)}")

            if total and done >= total:
                st.success("🎉 Verification complete.")
            elif st.session_state.get(f"{task}_verify_started"):
                st.info("⏳ In progress — click **Refresh Results** to update.")

            try:
                rows = coord_request(coord_url, coord_token, "GET", "/export").get("rows", [])
            except RuntimeError:
                rows = []

            if rows:
                res_df = pd.DataFrame(rows)
                st.markdown("#### 📇 Final verified contacts (Apollo format)")
                st.caption("Only people whose email **verified as deliverable** — your "
                           "original Apollo columns with the verified email filled in, "
                           "and no status columns.")
                apollo_df = build_apollo_output(
                    st.session_state.get(f"{task}_contacts_df"), res_df
                )
                if apollo_df.empty:
                    st.info("No deliverable emails yet — keep clicking **Refresh "
                            "Results** as verification progresses.")
                else:
                    st.dataframe(apollo_df, use_container_width=True, height=360)
                    st.download_button(
                        f"📥 Download Verified Contacts ({len(apollo_df)}) — Apollo format",
                        data=to_csv_bytes(apollo_df),
                        file_name=f"{task}_verified_contacts_apollo.csv",
                        mime="text/csv", type="primary",
                        use_container_width=True, key=f"{task}_dl_apollo",
                    )

                with st.expander("🔬 Full verification results (every candidate, all verdicts)"):
                    st.dataframe(res_df, use_container_width=True, height=400)
                    st.download_button(
                        f"📥 Download full results ({len(res_df)} rows)",
                        data=to_csv_bytes(res_df),
                        file_name=f"{task}_verification_results_full.csv",
                        mime="text/csv", use_container_width=True,
                        key=f"{task}_dl_full",
                    )


# ======================================================================
# Two independent tasks, each with its own page count and its own VPS pool.
# ======================================================================
def render_excel_tab():
    st.markdown(
        "Upload a **leadership Excel** (one row per company, role columns like "
        "MD/CEO/CTO). It dedupes people (nobody appears under two roles), drops "
        "role mailboxes (sales@/info@/…), and splits the work across **both** VPS "
        "pools running in parallel."
    )
    pools = _pools()
    if not pools:
        st.warning("No VPS pools configured — set `coordinator_url`/`coordinator_token` "
                   "(and `coordinator2_*`) in Secrets.")
    else:
        st.caption(f"🔗 {len(pools)} pool(s) connected: " + ", ".join(p[0] for p in pools))

    up = st.file_uploader("Upload leadership Excel (.xlsx)", type=["xlsx"], key="ex_up")
    if up is not None and st.button("📄 Process file", key="ex_proc"):
        with st.spinner("Reading, deduping, deriving domains…"):
            st.session_state["ex_r"] = excel_leads.process_excel(up)
        st.session_state.pop("ex_started", None)

    r = st.session_state.get("ex_r")
    if r is None or len(r) == 0:
        return

    ph = r["phase"].value_counts().to_dict()
    st.success(f"✅ {len(r)} unique people after dedup + role-email filtering.")
    c1, c2, c3 = st.columns(3)
    c1.metric("① Known emails", ph.get("known", 0))
    c2.metric("② Generate (domain known)", ph.get("generate", 0))
    c3.metric("③ Need domain (DeepSeek)", ph.get("needs_domain", 0))
    with st.expander("👀 Preview processed people"):
        st.dataframe(r.head(100), use_container_width=True, height=280)

    if not pools:
        return

    st.divider()
    st.markdown("#### ① Verify known emails (no generation — sent straight to verify)")
    if st.button(f"🚀 Send {ph.get('known', 0)} known emails to both pools",
                 key="ex_p1", type="primary"):
        n = _seed_split(excel_leads.known_queue(r), pools, clear=True)
        st.session_state["ex_started"] = True
        st.success(f"Queued {n} known emails, split across {len(pools)} pool(s).")

    st.markdown("#### ② Generate guesses & verify (company domain known)")
    if st.button(f"🎲 Generate + send for {ph.get('generate', 0)} people", key="ex_p2"):
        n = _seed_split(excel_leads.generated_queue(r, 5), pools, clear=False)
        st.session_state["ex_started"] = True
        st.success(f"Queued {n} generated candidates (added to the run).")

    st.markdown("#### ③ Find missing domains with DeepSeek, then generate & verify")
    dkey = _secret("deepseek_api_key")
    if not dkey:
        st.info(f"Add `deepseek_api_key` to Secrets to look up domains for the "
                f"{ph.get('needs_domain', 0)} remaining people.")
    elif st.button("🔍 Look up domains + generate + send", key="ex_p3"):
        nd = r[r["phase"] == "needs_domain"]
        companies = sorted(c for c in nd["company"].unique() if c)
        prog = st.progress(0.0, text="Asking DeepSeek…")
        mapping = excel_leads.deepseek_find_domains(
            companies, dkey,
            on_progress=lambda a, b: prog.progress(a / b, text=f"{a}/{b} companies"),
        )
        prog.empty()
        r2 = r.copy()
        for i in r2.index[r2["phase"] == "needs_domain"]:
            d = mapping.get(r2.at[i, "company"])
            if d:
                r2.at[i, "domain"] = d
                r2.at[i, "phase"] = "generate"
        st.session_state["ex_r"] = r2
        newly = r2[(r2["company"].isin(mapping.keys())) & (r2["domain"] != "")]
        n = _seed_split(excel_leads.generated_queue(newly, 5), pools, clear=False)
        st.session_state["ex_started"] = True
        st.success(f"DeepSeek found domains for {len(mapping)} companies; "
                   f"queued {n} candidates.")

    st.divider()
    if st.button("🔄 Refresh Results", key="ex_refresh") or st.session_state.get("ex_started"):
        sc, vc, rows = _combined_status(pools)
        total = sum(sc.values())
        done = sc.get("done", 0) + sc.get("error", 0)
        st.progress((done / total) if total else 0.0,
                    text=f"{done}/{total} addresses checked across both pools")
        m1, m2, m3 = st.columns(3)
        m1.metric("✅ Deliverable", vc.get("deliverable", 0) + vc.get("risky", 0))
        m2.metric("❌ Undeliverable", vc.get("undeliverable", 0))
        m3.metric("❔ Unknown", vc.get("unknown", 0))
        if total and done >= total:
            st.success("🎉 Verification complete.")
        if rows:
            apollo = build_apollo_output(r, pd.DataFrame(rows))
            st.markdown("#### 📇 Verified people (deliverable, one row per person)")
            if apollo.empty:
                st.info("No deliverable emails yet — click **Refresh Results** as it runs.")
            else:
                keep = [c for c in ["company", "designation", "name", "email", "domain"]
                        if c in apollo.columns]
                st.dataframe(apollo[keep] if keep else apollo,
                             use_container_width=True, height=340)
                st.download_button(
                    f"📥 Download verified contacts ({len(apollo)})",
                    data=to_csv_bytes(apollo[keep] if keep else apollo),
                    file_name="excel_verified_contacts.csv", mime="text/csv",
                    type="primary", use_container_width=True, key="ex_dl",
                )


task1_tab, task2_tab, excel_tab = st.tabs(["🅰️ Task 1", "🅱️ Task 2", "📤 Excel (both pools)"])

with task1_tab:
    render_task("t1", "Task 1", 25, "coordinator_url", "coordinator_token")

with task2_tab:
    render_task("t2", "Task 2", 50, "coordinator2_url", "coordinator2_token")

with excel_tab:
    render_excel_tab()


with st.expander("ℹ️ Instructions"):
    st.markdown("""
    ### Two independent tasks
    - **Task 1** has **25 pages** and uses its own VPS pool (Secrets:
      `coordinator_url` / `coordinator_token`).
    - **Task 2** has **50 pages** and uses a **separate** VPS pool (Secrets:
      `coordinator2_url` / `coordinator2_token`).
    - Both run **in parallel** — paste, generate and verify in each tab independently.

    ### In each task
    1. Paste raw Apollo JSON into the page tabs
    2. Click **Convert All Pages to CSV**
    3. In **Step 2**, choose emails-per-person and click **Generate Emails**
    4. In **Step 3**, click **Send to Verifier** — that task's VPS pool checks them
    5. Download **Verified Contacts (Apollo format)** — deliverable + risky only, no status columns
    """)
