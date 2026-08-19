#!/usr/bin/env bash
# Stop ONLY this tool's processes (coordinator + worker) on this VPS.
# It kills nothing except the exact PIDs recorded in coordinator.pid / worker.pid,
# so every other process on your shared VPS is left completely untouched.
#
#   cd /root/scraper && bash deploy/stop_vps.sh
set -euo pipefail
cd "$(dirname "$0")/.."

stop_pid() {
    local f="$1" pid
    if [ -f "$f" ]; then
        pid="$(cat "$f" 2>/dev/null || true)"
        if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            echo "stopped $f (pid $pid)"
        else
            echo "$f: not running"
        fi
        rm -f "$f"
    else
        echo "$f: no pid file (nothing to stop)"
    fi
}

stop_pid worker.pid
stop_pid coordinator.pid
echo "Done. Only this tool was stopped; nothing else on the VPS was affected."
