"""Coordinator: the single shared work queue for a multi-VPS verification run.

Run this on ONE machine (typically one of the VPSs). It seeds the verification
queue into a local SQLite DB and hands out work to the worker VPSs over HTTP,
leasing a whole domain to one node at a time and reclaiming work from any node
that dies. It does NOT probe anything itself - no port 25, no pool config needed.

    python coordinator.py \
        --input email_verification_queue.csv \
        --bind 10.0.0.1:8900 \
        --token "$VERIFY_TOKEN" \
        --state coordinator_state.db \
        --output results.csv          # written on Ctrl-C / shutdown

Then on each worker VPS (see verify_emails.py --coordinator):

    python verify_emails.py \
        --config verifier_config.json \
        --coordinator http://10.0.0.1:8900 \
        --token "$VERIFY_TOKEN" \
        --node-id vps2

SECURITY: the token is a shared bearer secret and traffic is plain HTTP. Run the
coordinator port ONLY on a private link (WireGuard/VPN) between your VPSs - never
expose it on a public interface. See deploy/COORDINATOR_SETUP.md.
"""

import argparse
import csv
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from verifier.leased_store import LeasedStore


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


class Handler(BaseHTTPRequestHandler):
    # Injected by make_handler.
    store = None
    token = None

    def log_message(self, *args):
        pass  # quiet; the reaper/progress lines are enough

    def _authed(self):
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        if header != expected:
            self._send(401, {"error": "bad token"})
            return False
        return True

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send(self, code, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # -- routing --------------------------------------------------------

    def do_GET(self):
        if not self._authed():
            return
        try:
            if self.path == "/counts":
                self._send(200, self.store.counts())
            elif self.path == "/export":
                self._send(200, {"rows": self.store.export_rows()})
            elif self.path == "/status":
                self._send(200, {
                    "counts": self.store.counts(),
                    "leases": self.store.lease_snapshot(),
                })
            elif self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # keep the server alive on a bad request
            self._send(500, {"error": str(exc)})

    def do_POST(self):
        if not self._authed():
            return
        try:
            body = self._body()
            if self.path == "/seed":
                # The Streamlit UI pushes a generated verification queue here.
                # `clear` starts a fresh batch (wipe prior candidates + leases).
                if body.get("clear"):
                    self.store.clear()
                candidates = body.get("candidates", [])
                added = self.store.seed(candidates)
                self._send(200, {"added": added, "received": len(candidates)})
            elif self.path == "/claim":
                rows = self.store.claim_for_node(
                    body["node_id"], int(body.get("limit", 1))
                )
                self._send(200, {"rows": rows})
            elif self.path == "/mark_done":
                self.store.mark_done(
                    body["email"], body["verdict"], body["confidence"],
                    body.get("rcpt_code"), body.get("catch_all"),
                    body.get("reasons", []), body.get("layers", []),
                    body["attempts"], body.get("last_ip"),
                )
                self._send(200, {"ok": True})
            elif self.path == "/mark_retry":
                self.store.mark_retry(
                    body["email"], body["attempts"],
                    body["next_attempt_at"], body.get("last_ip"),
                )
                self._send(200, {"ok": True})
            elif self.path == "/mark_error":
                self.store.mark_error(
                    body["email"], body.get("reasons", []),
                    body["attempts"], body.get("last_ip"),
                )
                self._send(200, {"ok": True})
            elif self.path == "/person_has_hit":
                self._send(200, {"hit": self.store.person_has_hit(body["row_id"])})
            elif self.path == "/has_open_work":
                self._send(200, {"open": self.store.has_open_work()})
            elif self.path == "/reset_stale_claims":
                self._send(200, {"reset": self.store.reset_node_claims(body["node_id"])})
            elif self.path == "/heartbeat":
                self.store.heartbeat(body["node_id"])
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except KeyError as exc:
            self._send(400, {"error": f"missing field {exc}"})
        except Exception as exc:
            self._send(500, {"error": str(exc)})


def make_handler(store, token):
    return type("BoundHandler", (Handler,), {"store": store, "token": token})


def reaper_loop(store, interval, stop_event):
    """Reclaim work from dead nodes on a fixed cadence."""
    while not stop_event.wait(interval):
        try:
            freed = store.reap_expired()
            if freed:
                print(f"\n[reaper] reclaimed {freed} stranded row(s) from a dead node")
        except Exception as exc:  # never let the reaper die silently
            print(f"\n[reaper] error: {exc}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verification work-queue coordinator")
    parser.add_argument("--input", required=True, help="Verification queue CSV to seed")
    parser.add_argument("--bind", default="0.0.0.0:8900", help="host:port to listen on")
    parser.add_argument("--token", required=True, help="Shared bearer token for workers")
    parser.add_argument("--state", default="coordinator_state.db", help="SQLite state DB")
    parser.add_argument("--output", help="Write results CSV here on shutdown")
    parser.add_argument("--lease-seconds", type=float, default=300.0,
                        help="How long a domain lease lives without a heartbeat")
    parser.add_argument("--reap-interval", type=float, default=30.0,
                        help="Seconds between dead-node reclaim sweeps")
    args = parser.parse_args(argv)

    host, _, port = args.bind.rpartition(":")
    host = host or "0.0.0.0"

    store = LeasedStore(args.state, lease_seconds=args.lease_seconds)
    candidates = read_queue(args.input)
    if not candidates:
        sys.exit("No candidate emails found in input.")
    added = store.seed(candidates)
    print(f"Seeded {added} new / {len(candidates)} total candidates into {args.state}")

    stop_event = threading.Event()
    reaper = threading.Thread(
        target=reaper_loop, args=(store, args.reap_interval, stop_event),
        name="reaper", daemon=True,
    )
    reaper.start()

    httpd = ThreadingHTTPServer((host, int(port)), make_handler(store, args.token))
    print(f"Coordinator listening on {host}:{port} "
          f"(lease {args.lease_seconds:.0f}s, reap every {args.reap_interval:.0f}s)")
    print("Point workers at it with: verify_emails.py --coordinator "
          f"http://{host}:{port} --token <token> --node-id <name>")

    def shutdown(*_):
        print("\nShutting down coordinator...")
        stop_event.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        httpd.serve_forever()
    finally:
        counts = store.counts()
        print(f"Status:  {counts['status']}")
        print(f"Verdict: {counts['verdict']}")
        if args.output:
            written = write_results(store, args.output)
            print(f"Wrote {written} rows to {args.output}")


if __name__ == "__main__":
    main()
