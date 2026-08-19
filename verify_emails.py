"""CLI runner for the 7-layer verifier.

Consumes the "Verification Queue" CSV produced by the Streamlit app and writes
a results CSV. Resumable: re-running against the same --state DB continues
where it left off.

    python verify_emails.py \
        --input email_verification_queue.csv \
        --config verifier_config.json \
        --output verified_results.csv

Run this ON THE VPS. Port 25 outbound must be open (most home/cloud ISPs block
it) and each source IP needs matching forward + reverse DNS for its EHLO name.
"""

import argparse
import csv
import sys
import time

from verifier import Config, RotationPool, StateStore, Verifier, RemoteStore


REQUIRED_COLUMNS = {"candidate_email"}


def read_queue(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"Input CSV missing required column(s): {', '.join(missing)}")
        rows = []
        for row in reader:
            email = (row.get("candidate_email") or "").strip()
            if not email:
                continue
            rows.append({
                "candidate_email": email,
                "row_id": _as_int(row.get("row_id")),
                "id": row.get("id", ""),
                "name": row.get("name", ""),
                "domain": (row.get("domain") or email.rsplit("@", 1)[-1]).lower(),
                "pattern": row.get("pattern", ""),
                "rank": _as_int(row.get("rank")),
                "is_known_email": str(row.get("is_known_email", "")).strip().lower()
                                  in ("1", "true", "yes"),
            })
        return rows


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def write_results(store, path):
    rows = store.export_rows()
    columns = [
        "row_id", "person_id", "name", "candidate_email", "domain", "pattern",
        "rank", "is_known", "verdict", "confidence", "rcpt_code", "catch_all",
        "attempts", "last_ip", "status", "reasons",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def make_progress_printer():
    counters = {"done": 0, "retry": 0, "skip": 0, "give_up": 0}
    last = [0.0]

    def on_event(event, **data):
        if event in counters:
            counters[event] += 1
        now = time.monotonic()
        if event == "recover":
            print(f"[resume] recovered {data['recovered']} in-flight candidate(s) from a prior run")
        elif event == "seed":
            print(f"[seed] {data['added']} new / {data['total']} total candidates queued")
        elif now - last[0] > 1.0 or event in ("give_up",):
            last[0] = now
            print(
                f"\r[progress] done={counters['done']} retry={counters['retry']} "
                f"skip={counters['skip']} gave_up={counters['give_up']}",
                end="", flush=True,
            )

    return on_event


def main(argv=None):
    parser = argparse.ArgumentParser(description="7-layer SMTP email verifier")
    parser.add_argument("--input", help="Verification queue CSV "
                        "(not needed in --coordinator mode; the coordinator holds the queue)")
    parser.add_argument("--config", default="verifier_config.json", help="Pool/rate config JSON")
    parser.add_argument("--output", default="verified_results.csv", help="Results CSV")
    parser.add_argument("--state", help="Override the SQLite state DB path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run layers 1-3 only (no SMTP sockets opened)")
    # Multi-VPS mode: pull work from a shared coordinator instead of a local DB.
    parser.add_argument("--coordinator", help="Coordinator base URL, e.g. http://10.0.0.1:8900")
    parser.add_argument("--token", help="Shared bearer token (with --coordinator)")
    parser.add_argument("--node-id", help="This VPS's name (with --coordinator); "
                        "defaults to the hostname")
    parser.add_argument("--once", action="store_true",
                        help="In --coordinator mode, exit when the queue drains "
                        "instead of staying up to wait for new work pushed from the UI")
    args = parser.parse_args(argv)

    # A dry run opens no sockets, so it doesn't need the IP pool configured yet.
    config = Config.load(args.config, require_pool=not args.dry_run)
    if args.state:
        config.state_db = args.state

    coordinated = bool(args.coordinator)
    if coordinated:
        if not args.token:
            sys.exit("--coordinator requires --token")
        import socket
        node_id = args.node_id or socket.gethostname()
        # The coordinator owns and seeds the queue, so a worker needs no input CSV
        # and never seeds locally.
        store = RemoteStore(args.coordinator, args.token, node_id,
                            on_event=make_progress_printer())
        store.start_heartbeat()
        candidates = None
        print(f"Worker '{node_id}' pulling from coordinator {args.coordinator}. "
              f"Pool: {len(config.source_ips)} IPs x {len(config.sender_identities)} identities.")
    else:
        if not args.input:
            sys.exit("--input is required (or use --coordinator for multi-VPS mode)")
        store = StateStore(config.state_db)
        candidates = read_queue(args.input)
        if not candidates:
            sys.exit("No candidate emails found in input.")
        print(f"Loaded {len(candidates)} candidates. "
              f"Pool: {len(config.source_ips)} IPs x {len(config.sender_identities)} identities.")

    # In coordinator mode a worker stays alive by default, idling until the UI
    # pushes a new batch. --once restores the old "exit when done" behaviour.
    keep_alive = coordinated and not args.once
    pool = RotationPool(config)
    verifier = Verifier(config, pool, store=store,
                        on_event=make_progress_printer(), offline=args.dry_run,
                        keep_alive=keep_alive)
    if keep_alive:
        print("Staying online, waiting for work from the coordinator. Ctrl-C to stop.")

    if args.dry_run:
        print("DRY RUN: SMTP layers 4-6 skipped (no sockets opened).")

    try:
        counts = verifier.run(candidates)
    except KeyboardInterrupt:
        verifier.stop()
        print("\nInterrupted - state saved, re-run to resume.")
        counts = store.counts()
    finally:
        if coordinated:
            store.close()

    written = write_results(store, args.output)
    print(f"\nWrote {written} rows to {args.output}")
    print(f"Status:  {counts['status']}")
    print(f"Verdict: {counts['verdict']}")


if __name__ == "__main__":
    main()
