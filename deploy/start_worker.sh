#!/usr/bin/env bash
# Start ONLY a worker on this VPS, connected to an existing coordinator running
# on another VPS. Use this on the 2nd (and later) VPSs. The 1st VPS uses
# start_vps.sh (which runs the coordinator + its own worker).
#
# Usage (fill COORD + TOKEN from the 1st VPS's start_vps.sh output):
#   cd /root/scraper
#   cp verifier_config.vps2.json verifier_config.json
#   COORD=http://187.127.179.167:8900 TOKEN=xxxxxxxx NODE=vps2 bash deploy/start_worker.sh
set -euo pipefail
cd "$(dirname "$0")/.."

: "${COORD:?set COORD=http://<coordinator-ip>:8900}"
: "${TOKEN:?set TOKEN=<the token printed by start_vps.sh on the 1st VPS>}"
NODE="${NODE:-vps2}"
CONFIG="${CONFIG:-verifier_config.json}"

echo "== Dependencies (private venv - system Python untouched) =="
apt-get update -y >/dev/null
apt-get install -y python3 python3-venv python3-pip curl >/dev/null
if [ ! -x venv/bin/python3 ]; then
    rm -rf venv
    python3 -m venv venv
fi
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: $CONFIG not found. Run:  cp verifier_config.vps2.json verifier_config.json"
    exit 1
fi

echo "== Confirming outbound port 25 =="
if (exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25) 2>/dev/null; then
    echo "OK: port 25 outbound works."; exec 3>&- 2>/dev/null || true
else
    echo "WARNING: port 25 looks blocked - results may all be 'unknown'."
fi

# Stop ONLY our own previous worker (by PID file), never a broad pkill.
if [ -f worker.pid ]; then
    p="$(cat worker.pid 2>/dev/null || true)"
    if [ -n "${p:-}" ] && kill -0 "$p" 2>/dev/null; then kill "$p" 2>/dev/null || true; fi
    rm -f worker.pid
fi
sleep 1

echo "== Starting worker '$NODE' -> $COORD =="
nohup ./venv/bin/python verify_emails.py --config "$CONFIG" \
    --coordinator "$COORD" --token "$TOKEN" --node-id "$NODE" \
    > worker.log 2>&1 &
echo $! > worker.pid
sleep 2

echo
echo "Worker '$NODE' is online and connected to the coordinator at $COORD."
echo "It will pick up work whenever you click Send to Verifier in the app."
echo "Watch it:  tail -f worker.log      Stop it:  pkill -f verify_emails.py"
