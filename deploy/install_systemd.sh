#!/usr/bin/env bash
# Install systemd services so the verifier auto-starts on boot and restarts if it
# ever crashes. Replaces the nohup processes from start_vps.sh / start_worker.sh.
# Run as root, from the repo root (/root/scraper).
#
# On the COORDINATOR machine (VPS2) - runs coordinator + a worker:
#   ROLE=coordinator NODE=vps2 bash deploy/install_systemd.sh
#
# On a WORKER machine (VPS3/4/5) - runs a worker that joins the coordinator:
#   ROLE=worker NODE=vps3 COORD=http://187.127.179.168:8900 TOKEN=xxxxx bash deploy/install_systemd.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
PY="$DIR/venv/bin/python"
ROLE="${ROLE:-worker}"
NODE="${NODE:-vps1}"
PORT="${PORT:-8900}"
CONFIG="${CONFIG:-verifier_config.json}"

[ -x "$PY" ] || { echo "ERROR: venv not found at $PY - run start_vps.sh once first."; exit 1; }
[ -f "$DIR/$CONFIG" ] || { echo "ERROR: $CONFIG missing - cp verifier_config.vpsN.json verifier_config.json"; exit 1; }

write_worker_unit() {
    local coord="$1" token="$2"
    cat > /etc/systemd/system/verifier-worker.service <<EOF
[Unit]
Description=Email Verifier Worker ($NODE)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$DIR
ExecStart=$PY $DIR/verify_emails.py --config $CONFIG --coordinator $coord --token $token --node-id $NODE
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

# Stop any nohup-launched instances so systemd is the sole owner of the port.
if [ -f "$DIR/coordinator.pid" ]; then kill "$(cat "$DIR/coordinator.pid")" 2>/dev/null || true; rm -f "$DIR/coordinator.pid"; fi
if [ -f "$DIR/worker.pid" ]; then kill "$(cat "$DIR/worker.pid")" 2>/dev/null || true; rm -f "$DIR/worker.pid"; fi
sleep 1

if [ "$ROLE" = "coordinator" ]; then
    if [ ! -f "$DIR/coordinator_token.txt" ]; then
        (openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n') > "$DIR/coordinator_token.txt"
    fi
    TOKEN="$(cat "$DIR/coordinator_token.txt")"
    [ -f "$DIR/seed.csv" ] || printf 'candidate_email\nplaceholder@init.local\n' > "$DIR/seed.csv"

    cat > /etc/systemd/system/verifier-coordinator.service <<EOF
[Unit]
Description=Email Verifier Coordinator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$DIR
ExecStart=$PY $DIR/coordinator.py --input seed.csv --bind 0.0.0.0:$PORT --token $TOKEN --state coordinator_state.db --output results.csv
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    write_worker_unit "http://127.0.0.1:$PORT" "$TOKEN"
    systemctl daemon-reload
    systemctl enable --now verifier-coordinator.service
    systemctl enable --now verifier-worker.service
    IP="$(curl -4 -s https://ifconfig.me || echo YOUR_VPS_IP)"
    echo
    echo "===============  COORDINATOR + WORKER installed (auto-start on)  ==============="
    echo "  App Coordinator URL:  http://$IP:$PORT"
    echo "  Token:                $TOKEN"
    echo "==============================================================================="
    echo "Status:  systemctl status verifier-coordinator verifier-worker"
    echo "Logs:    journalctl -u verifier-coordinator -f   (or verifier-worker)"
else
    : "${COORD:?set COORD=http://<coordinator-ip>:8900}"
    : "${TOKEN:?set TOKEN=<coordinator token>}"
    write_worker_unit "$COORD" "$TOKEN"
    systemctl daemon-reload
    systemctl enable --now verifier-worker.service
    echo
    echo "Worker '$NODE' installed and running (auto-start on boot) -> $COORD"
    echo "Status: systemctl status verifier-worker   |   Logs: journalctl -u verifier-worker -f"
fi
