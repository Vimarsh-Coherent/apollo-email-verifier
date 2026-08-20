import streamlit as st
import json
import urllib.request
import urllib.error
import pandas as pd
from io import StringIO

from email_patterns import EMAIL_PATTERNS, build_candidates

st.set_page_config(
    page_title="Apollo Scraper - JSON to CSV Converter",
    page_icon="📊",
    layout="wide"
)

st.title("📊 Apollo Scraper")
st.markdown("Paste JSON data from each Apollo page below. All data will be combined into a single CSV.")

# Helper function to extract people/contacts data from JSON (universal)
def extract_people_data(json_data):
    """Extract and flatten people/contacts data from JSON - handles both structures"""
    rows = []
    
    # Check for both 'people' and 'contacts' arrays
    data_list = json_data.get("people", []) or json_data.get("contacts", [])
    
    for person in data_list:
        # Extract basic person information
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
        
        # Extract email information (handles both direct email and contact_emails array)
        primary_email = person.get("email", "")
        email_status = person.get("email_status", "")
        email_true_status = person.get("email_true_status", "")
        
        # If contact_emails array exists, get primary email from there
        contact_emails = person.get("contact_emails", [])
        if contact_emails and not primary_email:
            primary_email = contact_emails[0].get("email", "")
            if not email_status:
                email_status = contact_emails[0].get("email_status", "")
            if not email_true_status:
                email_true_status = contact_emails[0].get("email_true_status", "")
        
        # Get all emails (for contacts with multiple emails)
        all_emails = [primary_email] if primary_email else []
        if contact_emails:
            all_emails.extend([e.get("email", "") for e in contact_emails if e.get("email") and e.get("email") != primary_email])
        all_emails = [e for e in all_emails if e]  # Remove empty strings
        
        row["email"] = primary_email
        row["email_status"] = email_status
        row["email_true_status"] = email_true_status
        row["all_emails"] = ", ".join(all_emails) if all_emails else ""
        
        # Extract phone numbers (handles both direct phone and phone_numbers array)
        phone_numbers = person.get("phone_numbers", [])
        if phone_numbers:
            primary_phone = phone_numbers[0].get("raw_number", "") or phone_numbers[0].get("sanitized_number", "")
            all_phones = [p.get("raw_number", "") or p.get("sanitized_number", "") for p in phone_numbers if p.get("raw_number") or p.get("sanitized_number")]
            all_phones = [p for p in all_phones if p]  # Remove empty strings
            row["phone"] = primary_phone
            row["all_phones"] = ", ".join(all_phones) if all_phones else ""
        else:
            # Fallback to direct phone fields
            row["phone"] = person.get("phone", "") or person.get("sanitized_phone", "")
            row["all_phones"] = row["phone"]
        
        # Extract organization details (handles both nested organization object and direct fields)
        org = person.get("organization", {})
        if org:
            # Nested organization object (people structure)
            row["org_name"] = org.get("name", "") or person.get("organization_name", "")
            row["org_website"] = org.get("website_url", "")
            row["org_linkedin"] = org.get("linkedin_url", "")
            row["org_employees"] = org.get("estimated_num_employees", "")
            row["org_industries"] = ", ".join(org.get("industries", [])) if isinstance(org.get("industries"), list) else ""
            row["org_keywords"] = ", ".join(org.get("keywords", [])) if isinstance(org.get("keywords"), list) else ""
            row["org_phone"] = org.get("phone", "") or org.get("sanitized_phone", "")
            row["org_founded_year"] = org.get("founded_year", "")
        else:
            # Direct organization fields (contacts structure)
            row["org_name"] = person.get("organization_name", "")
            row["org_website"] = ""
            row["org_linkedin"] = ""
            row["org_employees"] = ""
            row["org_industries"] = ""
            row["org_keywords"] = ""
            row["org_phone"] = ""
            row["org_founded_year"] = ""
        
        # Additional fields that might be useful
        row["twitter_url"] = person.get("twitter_url", "")
        row["facebook_url"] = person.get("facebook_url", "")
        row["person_id"] = person.get("person_id", "")
        row["account_id"] = person.get("account_id", "")
        row["created_at"] = person.get("created_at", "")
        row["updated_at"] = person.get("updated_at", "")
        
        rows.append(row)
    
    return rows

# Create tabs for better organization
num_pages = 25
tabs = st.tabs([f"Page {i+1}" for i in range(num_pages)])

# Store all JSON inputs
json_inputs = {}

# Create text areas in each tab
for i, tab in enumerate(tabs):
    with tab:
        page_num = i + 1
        json_input = st.text_area(
            f"Paste JSON data from Page {page_num}",
            height=400,
            placeholder=f'Paste your JSON data from page {page_num} here...',
            key=f"page_{page_num}"
        )
        json_inputs[page_num] = json_input
        
        # Show character count
        if json_input:
            char_count = len(json_input)
            st.caption(f"📝 {char_count} characters")

# Convert button
if st.button("🔄 Convert All Pages to CSV", type="primary", use_container_width=True):
    all_rows = []
    pages_processed = 0
    pages_with_errors = []
    total_people = 0
    
    # Process each page
    for page_num in range(1, num_pages + 1):
        json_input = json_inputs.get(page_num, "")
        
        if json_input.strip():
            try:
                # Parse JSON
                data = json.loads(json_input)
                
                # Extract people data
                rows = extract_people_data(data)
                
                if rows:
                    all_rows.extend(rows)
                    pages_processed += 1
                    total_people += len(rows)
                    st.success(f"✅ Page {page_num}: {len(rows)} records extracted")
                else:
                    st.warning(f"⚠️ Page {page_num}: No people/contacts data found")
                    
            except json.JSONDecodeError as e:
                error_msg = f"❌ Page {page_num}: Invalid JSON format - {str(e)}"
                st.error(error_msg)
                pages_with_errors.append(page_num)
            except Exception as e:
                error_msg = f"❌ Page {page_num}: Error - {str(e)}"
                st.error(error_msg)
                pages_with_errors.append(page_num)
    
    # Persist results so later widgets (the email generator) don't wipe them.
    # Streamlit reruns the whole script on every interaction, which would make
    # this `if st.button(...)` block False and discard everything.
    if all_rows:
        st.session_state["contacts_df"] = pd.DataFrame(all_rows)
        st.session_state["pages_processed"] = pages_processed
        st.session_state["pages_with_errors"] = pages_with_errors
        # Any previously generated emails belong to the old dataset.
        st.session_state.pop("emails_wide", None)
        st.session_state.pop("emails_long", None)
        st.session_state.pop("emails_stats", None)
    else:
        st.session_state.pop("contacts_df", None)
        st.error("❌ No data found in any of the pages. Please paste JSON data in at least one page.")


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


# Priority for choosing one best address per person.
_VERDICT_ORDER = {"deliverable": 0, "risky": 1, "unknown": 2, "undeliverable": 3}


def best_per_person(res_df):
    """Collapse to one row per person: best verdict, then highest confidence."""
    if res_df.empty:
        return res_df
    d = res_df.copy()
    d["_v"] = d.get("verdict", "").map(lambda v: _VERDICT_ORDER.get(v, 4))
    d["_conf"] = pd.to_numeric(d.get("confidence"), errors="coerce").fillna(0)
    d["_rank"] = pd.to_numeric(d.get("rank"), errors="coerce").fillna(999)
    d = d.sort_values(["row_id", "_v", "_conf", "_rank"],
                      ascending=[True, True, False, True])
    best = d.groupby("row_id", as_index=False).first()
    return best.drop(columns=["_v", "_conf", "_rank"], errors="ignore")


df = st.session_state.get("contacts_df")

if df is not None:
    pages_processed = st.session_state.get("pages_processed", 0)
    pages_with_errors = st.session_state.get("pages_with_errors", [])

    # Display summary
    st.divider()
    st.success(f"🎉 Successfully processed {pages_processed} page(s) with {len(df)} total records!")
    st.info(f"📊 Total records in CSV: {len(df)} (all records included, no duplicates removed)")

    if pages_with_errors:
        st.warning(f"⚠️ {len(pages_with_errors)} page(s) had errors: {', '.join(map(str, pages_with_errors))}")

    # Display preview
    st.subheader("📋 Data Preview")
    st.dataframe(df, use_container_width=True, height=400)

    # Download button
    st.download_button(
        label=f"📥 Download CSV ({len(df)} records)",
        data=to_csv_bytes(df),
        file_name="linkedin_contacts_combined.csv",
        mime="text/csv",
        use_container_width=True
    )

    # Show statistics
    st.subheader("📊 Statistics")
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Total Records", len(df))
    with col2:
        st.metric("Pages Processed", pages_processed)
    with col3:
        st.metric("With Email", len(df[df["email"] != ""]))
    with col4:
        if "email_status" in df.columns:
            verified_count = len(df[df["email_status"].astype(str).str.lower() == "verified"])
        else:
            verified_count = 0
        st.metric("Verified Emails", verified_count)
    with col5:
        st.metric("Unique Countries", df["country"].nunique())

    # Additional stats
    col6, col7, col8 = st.columns(3)
    with col6:
        st.metric("Unique Companies", df["org_name"].nunique() if "org_name" in df.columns else 0)
    with col7:
        c_suite_count = len(df[df["seniority"] == "c_suite"])
        st.metric("C-Suite", c_suite_count)
    with col8:
        vp_count = len(df[df["seniority"] == "vp"])
        st.metric("Vice Presidents", vp_count)

    # ------------------------------------------------------------------
    # Step 2 - Email permutation generator
    # ------------------------------------------------------------------
    st.divider()
    st.subheader("✉️ Step 2: Generate Email Permutations")
    st.markdown(
        "Generate multiple candidate addresses per person using randomized "
        "naming patterns. The output feeds the SMTP verification pipeline."
    )

    emails_per_person = st.slider(
        "Emails to generate per person",
        min_value=2,
        max_value=7,
        value=5,
        help="Each person gets this many distinct addresses, drawn from the pattern catalogue."
    )

    opt_col1, opt_col2 = st.columns(2)
    with opt_col1:
        reproducible = st.checkbox(
            "Reproducible pattern picks",
            value=True,
            help="Seed the randomizer per person so re-running gives the same addresses. "
                 "Uncheck for a fresh random draw every run."
        )
    with opt_col2:
        salt = st.number_input(
            "Shuffle seed",
            min_value=0,
            max_value=9999,
            value=0,
            step=1,
            help="Change this to reshuffle which patterns each person receives."
        )

    if st.button("🎲 Generate Emails", type="primary", use_container_width=True):
        wide, long, stats = build_candidates(
            df, emails_per_person, reproducible=reproducible, salt=int(salt)
        )
        st.session_state["emails_wide"] = pd.DataFrame(wide)
        st.session_state["emails_long"] = pd.DataFrame(long)
        st.session_state["emails_stats"] = stats

    wide_df = st.session_state.get("emails_wide")
    long_df = st.session_state.get("emails_long")
    stats = st.session_state.get("emails_stats")

    if wide_df is not None and stats is not None:
        if stats["candidates_total"] == 0:
            st.error(
                "❌ No candidates generated. No usable company domain was found — "
                "these records need either a corporate email or an `org_website`."
            )
        else:
            st.success(
                f"✅ Generated {stats['candidates_total']} candidate emails "
                f"for {stats['people_with_domain']} people across "
                f"{stats['unique_domains']} domains."
            )

            m1, m2, m3, m4 = st.columns(4)
            with m1:
                st.metric("Candidate Emails", stats["candidates_total"])
            with m2:
                st.metric("People Covered", stats["people_with_domain"])
            with m3:
                st.metric("Unique Domains", stats["unique_domains"])
            with m4:
                skipped = (stats["people_skipped_no_domain"]
                           + stats["people_skipped_free_email"]
                           + stats["people_skipped_no_name"])
                st.metric("Skipped", skipped)

            if skipped:
                st.warning(
                    f"⚠️ Skipped {skipped} record(s): "
                    f"{stats['people_skipped_no_domain']} without a company domain, "
                    f"{stats['people_skipped_free_email']} with only a free-mail address "
                    f"(gmail/yahoo/etc. — permutations there are worthless and get your IP blocked), "
                    f"{stats['people_skipped_no_name']} without a usable name."
                )

            tab_wide, tab_long, tab_patterns = st.tabs(
                ["📋 Per Person", "🔍 Verification Queue", "🧩 Pattern Usage"]
            )

            with tab_wide:
                st.dataframe(wide_df, use_container_width=True, height=400)
                st.download_button(
                    label=f"📥 Download Per-Person CSV ({len(wide_df)} rows)",
                    data=to_csv_bytes(wide_df),
                    file_name=f"emails_per_person_{emails_per_person}x.csv",
                    mime="text/csv",
                    use_container_width=True
                )

            with tab_long:
                st.caption(
                    "One row per candidate address — this is the file to hand to the "
                    "SMTP verification workers."
                )
                st.dataframe(long_df, use_container_width=True, height=400)
                st.download_button(
                    label=f"📥 Download Verification Queue ({len(long_df)} rows)",
                    data=to_csv_bytes(long_df),
                    file_name="email_verification_queue.csv",
                    mime="text/csv",
                    use_container_width=True
                )

            with tab_patterns:
                counts = long_df["pattern"].value_counts().rename_axis("pattern")
                counts = counts.reset_index(name="times_used")
                st.dataframe(counts, use_container_width=True, height=400)
                st.caption(
                    f"{len(EMAIL_PATTERNS)} patterns in the catalogue. Weighting means "
                    "common conventions (first.last, flast) dominate while rarer ones "
                    "still appear, so the mix differs person to person."
                )

    # ------------------------------------------------------------------
    # Step 3 - Verify the candidate queue on the VPS pool (coordinator)
    # ------------------------------------------------------------------
    long_df = st.session_state.get("emails_long")
    if long_df is not None and len(long_df) > 0:
        st.divider()
        st.subheader("🔎 Step 3: Verify Emails")
        st.markdown(
            "Send the candidate queue to your VPS verification pool. One "
            "**coordinator** leases each domain to a single VPS, checks the "
            "mailboxes over SMTP, and reclaims work automatically if a VPS drops. "
            "Progress and verified results stream back here."
        )

        # The coordinator URL + token live in the app's Secrets. When they're
        # set, the connection is hidden from the UI entirely; the fields only
        # appear as a fallback when Secrets aren't configured (e.g. local dev).
        secret_url = _secret("coordinator_url")
        secret_token = _secret("coordinator_token")
        if secret_url and secret_token:
            coord_url = secret_url
            coord_token = secret_token
            st.caption("🔗 Connected to your VPS verification pool.")
        else:
            cc1, cc2 = st.columns([2, 1])
            with cc1:
                coord_url = st.text_input(
                    "Coordinator URL",
                    value=st.session_state.get("coord_url", ""),
                    placeholder="http://10.0.0.1:8900",
                )
            with cc2:
                coord_token = st.text_input(
                    "Token",
                    value=st.session_state.get("coord_token", ""),
                    type="password",
                )

        ready = bool(coord_url and coord_token)
        vb1, vb2 = st.columns(2)
        with vb1:
            start_clicked = st.button(
                "🚀 Send to Verifier & Start", type="primary",
                use_container_width=True, disabled=not ready,
            )
        with vb2:
            refresh_clicked = st.button(
                "🔄 Refresh Results", use_container_width=True, disabled=not ready,
            )

        if not ready:
            st.info("Enter the coordinator URL and token to enable verification. "
                    "On Streamlit Cloud you can preset these as `coordinator_url` "
                    "and `coordinator_token` in the app's Secrets.")

        if start_clicked:
            st.session_state["coord_url"] = coord_url
            st.session_state["coord_token"] = coord_token
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
                chunk = 1000
                total_added = 0
                prog = st.progress(0.0, text="Sending queue to coordinator...")
                for i in range(0, len(payload), chunk):
                    part = payload[i:i + chunk]
                    resp = coord_request(
                        coord_url, coord_token, "POST", "/seed",
                        {"candidates": part, "clear": i == 0},
                    )
                    total_added += resp.get("added", 0)
                    prog.progress(
                        min(1.0, (i + len(part)) / len(payload)),
                        text=f"Sent {i + len(part)}/{len(payload)} addresses",
                    )
                prog.empty()
                st.session_state["verify_started"] = True
                st.success(
                    f"✅ Queued {total_added} addresses on the coordinator. Your VPS "
                    "workers will pick them up. Use **Refresh Results** to track progress."
                )
            except RuntimeError as exc:
                st.error(f"❌ {exc}")

        if (refresh_clicked or st.session_state.get("verify_started")) and ready:
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

                st.progress(
                    (done / total) if total else 0.0,
                    text=f"{done}/{total} addresses checked",
                )

                sm1, sm2, sm3, sm4 = st.columns(4)
                sm1.metric("✅ Deliverable", by_verdict.get("deliverable", 0))
                sm2.metric("⚠️ Risky", by_verdict.get("risky", 0))
                sm3.metric("❌ Undeliverable", by_verdict.get("undeliverable", 0))
                sm4.metric("❔ Unknown", by_verdict.get("unknown", 0))

                leases = status.get("leases", [])
                if leases:
                    nodes = sorted({l["node"] for l in leases})
                    st.caption(
                        f"🖥️ Working now: **{', '.join(nodes)}** — "
                        f"{len(leases)} domain(s) leased · "
                        f"pending {by_status.get('pending', 0)} · "
                        f"in-flight {by_status.get('claimed', 0)} · "
                        f"retry {by_status.get('retry', 0)}"
                    )

                if total and done >= total:
                    st.success("🎉 Verification complete.")
                elif st.session_state.get("verify_started"):
                    st.info("⏳ In progress — click **Refresh Results** to update.")

                try:
                    rows = coord_request(coord_url, coord_token, "GET", "/export").get("rows", [])
                except RuntimeError:
                    rows = []

                if rows:
                    res_df = pd.DataFrame(rows)

                    st.markdown("#### 🎯 Best email per person")
                    st.caption("One row per person — the strongest verdict, then "
                               "highest confidence.")
                    best_df = best_per_person(res_df)
                    view_cols = [c for c in ["name", "org_name", "candidate_email",
                                             "verdict", "confidence", "domain"]
                                 if c in best_df.columns]
                    st.dataframe(best_df[view_cols] if view_cols else best_df,
                                 use_container_width=True, height=300)

                    deliverable = best_df[best_df["verdict"] == "deliverable"] \
                        if "verdict" in best_df.columns else best_df.iloc[0:0]
                    st.download_button(
                        f"📥 Download deliverable emails ({len(deliverable)})",
                        data=to_csv_bytes(deliverable),
                        file_name="verified_deliverable.csv",
                        mime="text/csv",
                        use_container_width=True,
                        disabled=len(deliverable) == 0,
                    )

                    with st.expander("🔬 Full verification results (every candidate)"):
                        st.dataframe(res_df, use_container_width=True, height=400)
                        st.download_button(
                            f"📥 Download full results ({len(res_df)} rows)",
                            data=to_csv_bytes(res_df),
                            file_name="verification_results_full.csv",
                            mime="text/csv",
                            use_container_width=True,
                        )

# Instructions
with st.expander("ℹ️ Instructions"):
    st.markdown("""
    ### How to use:
    1. Navigate through the page tabs (Page 1, Page 2, etc.)
    2. Copy the raw JSON data from each Apollo page
    3. Paste it into the corresponding page tab
    4. Repeat for all pages you want to include
    5. Click "Convert All Pages to CSV" button
    6. Review the preview and statistics
    7. Click "Download CSV" to save the combined file
    8. In **Step 2**, pick how many emails to generate per person (2–7) and click "Generate Emails"
    9. Download the **Verification Queue** CSV — that is the file the SMTP verifier consumes

    ### Email generation:
    - Each person receives 2–7 distinct addresses built from their name plus their company domain
    - The domain comes from their existing corporate email first, then `org_website`
    - Free-mail records (gmail, yahoo, outlook…) are skipped — permutations there never land and
      probing those servers gets your sending IP blocked
    - Patterns are drawn by **weighted random sampling**, so common conventions
      (`first.last`, `flast`) show up most often but the exact mix varies per person
    - Accents are stripped (`José` → `jose`) and multi-part surnames are joined
      (`Van Der Berg` → `vanderberg`)
    - "Reproducible" keeps the draw stable across reruns; change the shuffle seed to redraw

    ### Features:
    - ✅ Combines data from multiple pages
    - ✅ Shows processing status for each page
    - ✅ Displays comprehensive statistics
    - ✅ Handles errors gracefully (skips invalid pages)
    - ✅ All records included (no duplicates removed)
    
    ### Supported JSON formats:
    The app automatically detects and handles both formats:
    
    **Format 1 - People structure:**
    - JSON with a `people` array
    - Nested `organization` object with company details
    - Direct email and phone fields
    
    **Format 2 - Contacts structure:**
    - JSON with a `contacts` array
    - `contact_emails` array for multiple emails
    - `phone_numbers` array for multiple phones
    - Direct `organization_name` field
    
    Both formats are automatically detected and converted to a unified CSV structure.
    
    ### Tips:
    - You don't need to fill all 25 pages - only paste data in the pages you have
    - Empty pages will be skipped automatically
    - All records are included in the CSV (no duplicates removed)
    """)
