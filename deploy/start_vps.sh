#!/usr/bin/env bash
# One-time launcher for a single VPS, UI-driven verification.
#
# SAFE FOR A SHARED VPS. This script:
#   * installs nothing system-wide except (if missing) python3/venv/pip/curl;
#     all Python packages go into a private ./venv, so your other apps' Python
#     is never touched.
#   * only ever stops processes IT started, tracked by *.pid files - it never
#     does a broad `pkill`, so your other processes are safe.
#   * does NOT touch the firewall, does NOT reboot, does NOT run apt upgrade.
#   * uses one folder (/root/scraper) and one port (default 8900).
#
#   cd /root/scraper
#   cp verifier_config.vps1.json verifier_config.json
#   bash deploy/start_vps.sh
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG="${CONFIG:-verifier_config.json}"
NODE="${NODE:-vps1}"
PORT="${PORT:-8900}"          # change if 8900 is already used by another app

# --- stop ONLY our own previous run (never a broad pkill) ------------------
stop_pid() {
    local f="$1" pid
    if [ -f "$f" ]; then
        pid="$(cat "$f" 2>/dev/null || true)"
        if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$f"
    fi
}

# --- refuse to steal a port another app is already using -------------------
port_in_use_by_other() {
    # true if something OTHER than our tracked coordinator holds the port
    local holder
    holder="$( (ss -ltnp 2>/dev/null || true) | grep -E "[:.]$PORT[[:space:]]" || true )"
    [ -n "$holder" ]
}

echo "== Dependencies (private venv - system Python untouched) =="
apt-get update -y >/dev/null
# python3-venv is required for `python3 -m venv` to work on Debian/Ubuntu even
# when the `venv` module imports - it ships ensurepip separately.
apt-get install -y python3 python3-venv python3-pip curl >/dev/null
# (Re)create the venv if it's missing or a previous run left it half-made.
if [ ! -x venv/bin/python3 ]; then
    rm -rf venv
    python3 -m venv venv
fi
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: $CONFIG not found. Run:  cp verifier_config.vps1.json verifier_config.json"
    exit 1
fi

echo "== Confirming outbound port 25 =="
if (exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25) 2>/dev/null; then
    echo "   OK: port 25 outbound works."; exec 3>&- 2>/dev/null || true
else
    echo "   WARNING: port 25 looks blocked - results may all be 'unknown'."
fi

# token, persisted so re-runs keep the same one
if [ ! -f coordinator_token.txt ]; then
    (openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n') \
        > coordinator_token.txt
fi
TOKEN="$(cat coordinator_token.txt)"
IP="$(curl -4 -s https://ifconfig.me || echo YOUR_VPS_IP)"

echo "== Stopping only our own previous run (if any) =="
stop_pid coordinator.pid
stop_pid worker.pid
sleep 1

if port_in_use_by_other; then
    echo "ERROR: port $PORT is already in use by another program on this VPS."
    echo "       Pick a free port, e.g.:  PORT=8950 bash deploy/start_vps.sh"
    echo "       (and use that same port in the app's Coordinator URL)"
    exit 1
fi

printf 'candidate_email\nplaceholder@init.local\n' > seed.csv

echo "== Starting coordinator on 0.0.0.0:$PORT =="
nohup ./venv/bin/python coordinator.py --input seed.csv --bind "0.0.0.0:$PORT" \
    --token "$TOKEN" --state coordinator_state.db --output results.csv \
    > coordinator.log 2>&1 &
echo $! > coordinator.pid
sleep 3

echo "== Starting worker '$NODE' =="
nohup ./venv/bin/python verify_emails.py --config "$CONFIG" \
    --coordinator "http://127.0.0.1:$PORT" --token "$TOKEN" --node-id "$NODE" \
    > worker.log 2>&1 &
echo $! > worker.pid
sleep 2

echo
echo "=================  PASTE THESE INTO THE APP (Step 3)  ================="
echo "   Coordinator URL:  http://$IP:$PORT"
echo "   Token:            $TOKEN"
echo "======================================================================"
echo
echo "To stop ONLY this tool (nothing else on the VPS):  bash deploy/stop_vps.sh"
echo "Watch progress:  tail -f worker.log"
echo
echo "NOTE on security: port $PORT is reachable from the internet (token-protected)."
echo "  Do NOT run 'ufw enable' on this shared box - it could block your other"
echo "  services. If you want to restrict $PORT, add ONE rule in the Hostinger"
echo "  firewall panel (allow $PORT only from your PC's IP), which won't affect"
echo "  anything else on the server."
