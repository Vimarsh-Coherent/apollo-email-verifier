#!/usr/bin/env bash
# Bootstrap one Hostinger VPS for the email verifier.
# Run from the repo root:  bash deploy/bootstrap.sh
set -euo pipefail

echo "== Installing packages =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git netcat-openbsd dnsutils wireguard curl

echo
echo "== Gate: outbound port 25 =="
if nc -zv -w8 gmail-smtp-in.l.google.com 25 2>&1 | grep -qiE 'open|succeeded'; then
    echo "OK: port 25 outbound is open."
else
    echo "FAIL: port 25 is blocked. Open a Hostinger support ticket to unblock"
    echo "      outbound SMTP before continuing - verification cannot work otherwise."
    exit 1
fi

echo
echo "== Forward/reverse DNS for this IP =="
IP="$(curl -s https://ifconfig.me || true)"
PTR="$(dig -x "$IP" +short || true)"
echo "  public IP : $IP"
echo "  rDNS (PTR): ${PTR:-<none>}"
if [ -n "$PTR" ]; then
    FWD="$(dig +short "${PTR%.}" || true)"
    echo "  forward   : ${PTR%.} -> ${FWD:-<none>}"
    if [ "$FWD" = "$IP" ]; then
        echo "OK: forward and reverse DNS match (use ${PTR%.} as the EHLO name)."
    else
        echo "WARN: forward/reverse do not match - strict MX servers may reject."
    fi
else
    echo "WARN: no PTR set. Set reverse DNS in the Hostinger hPanel."
fi

echo
echo "== Python environment =="
python3 -m venv venv
./venv/bin/pip install --upgrade pip >/dev/null
./venv/bin/pip install -r requirements.txt

echo
echo "Done. Next:"
echo "  1) cp verifier_config.vpsN.json verifier_config.json   (N = this box)"
echo "  2) set up WireGuard (see deploy/WIREGUARD_SETUP.md)"
echo "  3) launch the coordinator and/or worker (see deploy/COORDINATOR_SETUP.md)"
