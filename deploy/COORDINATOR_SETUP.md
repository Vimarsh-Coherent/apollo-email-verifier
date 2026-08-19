# Multi-VPS verification — coordinator setup

Run the verifier across **5 VPSs (1 IP each)** as one shared job. A single
**coordinator** process owns the work queue and hands each *domain* to one VPS at
a time; the other VPSs pull work from it over HTTP. If a VPS dies, its work is
automatically reclaimed by the others.

This gives you exactly:

- **Each email verified by exactly one VPS** — a whole domain is leased to one
  node, so no two VPSs ever touch the same address (or the same domain).
- **All 5 VPSs working in parallel** — each pulls a different domain.
- **Correct rate limiting** — because one VPS owns a domain, its local limiter
  enforces the per-domain spacing/caps; no cross-machine collisions.
- **Automatic failover** — a dead VPS's in-flight work is freed and picked up by
  another after its lease expires.

Throughput is unchanged by all this: **5 IPs ≈ 750 checks/hour ≈ 12.5/min.**
Coordination buys resilience and no-double-work, not more speed.

---

## Architecture

```
                 ┌─────────────────────────┐
                 │  Coordinator (1 process)│   holds the SQLite queue,
                 │  coordinator.py :8900   │   leases domains, reaps dead nodes
                 └───────────┬─────────────┘
        private link (WireGuard / VPN), never public
      ┌──────────┬──────────┼──────────┬──────────┐
   ┌──▼──┐    ┌──▼──┐    ┌──▼──┐    ┌──▼──┐    ┌──▼──┐
   │VPS1 │    │VPS2 │    │VPS3 │    │VPS4 │    │VPS5 │   each runs verify_emails.py
   │1 IP │    │1 IP │    │1 IP │    │1 IP │    │1 IP │   --coordinator, its own IP
   └─────┘    └─────┘    └─────┘    └─────┘    └─────┘
```

The coordinator does **no** SMTP itself — it needs no port 25 and no pool config.
The workers do the probing, each binding to its own IP (see `VPS_SETUP.md` for the
port 25 + forward/reverse DNS requirements that still apply per VPS).

---

## Step 1 — Private network between the VPSs (required)

The coordinator port carries a bearer token in plain HTTP. **Never expose it on a
public interface.** Put all 5 VPSs on a WireGuard mesh (or your provider's private
network) and bind the coordinator to that private IP.

Minimal WireGuard: give each VPS a `10.0.0.x` address, then verify every worker
can reach the coordinator's private IP:

```bash
# from each worker VPS, with the coordinator at 10.0.0.1
nc -zv 10.0.0.1 8900
```

## Step 2 — Start the coordinator (on one VPS)

Pick one VPS (or a small separate box) to be the coordinator. It needs the repo
and the verification-queue CSV exported from the Streamlit app.

```bash
export VERIFY_TOKEN="$(openssl rand -hex 24)"     # share this with the workers

python coordinator.py \
    --input email_verification_queue.csv \
    --bind 10.0.0.1:8900 \
    --token "$VERIFY_TOKEN" \
    --state coordinator_state.db \
    --output results.csv \
    --lease-seconds 300 \
    --reap-interval 30
```

- `--input` seeds the queue. (You can also push a queue from the Streamlit UI — see
  Step 4 — in which case any small placeholder CSV works here.)
- `--lease-seconds 300` — a domain a worker is holding is considered abandoned if it
  goes 300s without a heartbeat. Workers heartbeat every ~100s, so this tolerates
  brief blips without false failover.
- `--output` is written when you stop the coordinator (Ctrl-C).
- State is durable in SQLite: restart the coordinator and it resumes.

Leave it running (tmux, or a systemd unit modeled on `verifier.service`).

## Step 3 — Start a worker on each VPS

On **every** VPS (including, optionally, the coordinator box), run the verifier in
coordinator mode. Each keeps its own `verifier_config.json` with **its own single
IP** in `source_ips` and the matching `ehlo_by_ip`.

```bash
python verify_emails.py \
    --config verifier_config.json \
    --coordinator http://10.0.0.1:8900 \
    --token "$VERIFY_TOKEN" \
    --node-id vps2 \
    --output vps2_local_copy.csv
```

- `--node-id` must be **unique per VPS** (e.g. `vps1`…`vps5`). It's how the
  coordinator tracks who owns which domain and whose work to reclaim on death.
- No `--input` needed — the coordinator hands out the work.
- Workers can be started/stopped/restarted freely; a restarted worker reclaims
  only its own prior in-flight rows, and a permanently-dead one's work is reaped.

Do a `--dry-run` first (opens no sockets) to confirm the worker can reach the
coordinator:

```bash
python verify_emails.py --config verifier_config.json \
    --coordinator http://10.0.0.1:8900 --token "$VERIFY_TOKEN" \
    --node-id vps2 --dry-run
```

## Step 4 — (Optional) Drive it from the Streamlit UI

The app's **Step 3: Verify Emails** section pushes the generated queue straight to
the coordinator and shows live progress + verified results. Point it at the
coordinator by setting, in the app's **Secrets** (or the on-screen fields):

```toml
coordinator_url = "http://10.0.0.1:8900"
coordinator_token = "the-same-VERIFY_TOKEN"
```

The Streamlit app must itself be able to reach the coordinator's address — if you
run the app on your laptop, join it to the same WireGuard network. Do **not** move
the coordinator to a public IP just to reach it from Streamlit Cloud.

---

## Watching progress

```bash
# from any box on the private network
curl -s -H "Authorization: Bearer $VERIFY_TOKEN" http://10.0.0.1:8900/status | python -m json.tool
```

Shows counts by status/verdict and which node currently holds which domain lease.
`/export` returns every row; `/counts` just the totals.

## Endpoints (all require `Authorization: Bearer <token>`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/seed` | Push candidates (`{"candidates":[...], "clear":true}`) — used by the UI |
| POST | `/claim` | Worker leases a domain and claims its due rows |
| POST | `/mark_done`, `/mark_retry`, `/mark_error` | Worker writes a verdict |
| POST | `/heartbeat` | Worker keeps its leases alive |
| POST | `/reset_stale_claims` | Worker startup recovery (its own rows only) |
| POST | `/person_has_hit`, `/has_open_work` | Worker queries |
| GET | `/status`, `/counts`, `/export`, `/health` | Monitoring / results |

## Tuning failover speed

`--lease-seconds` is the trade-off: lower = faster reclaim of a dead node's work,
but too low risks reclaiming work from a live-but-slow node (which then wastes a
few probes redoing it). 300s is a safe default. The worker heartbeat interval is
lease/3 automatically, so keep the lease at least ~30s.
```
