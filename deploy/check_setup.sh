#!/usr/bin/env bash
# Pre-flight check for the email verifier VPS.
#
# Reads verifier_config.json and verifies the two things that actually decide
# whether verification will work:
#   1. Port 25 outbound is open (can we reach a real MX?)
#   2. Each source IP has matching forward + reverse DNS for its EHLO hostname
#
# Run this AFTER you have added the IPs and set up DNS, and BEFORE the first
# live run. Exits non-zero if anything critical fails.
#
#   ./deploy/check_setup.sh [path-to-config]     (default: verifier_config.json)

set -u
CONFIG="${1:-verifier_config.json}"
FAIL=0
WARN=0

green() { printf '\033[32m%s\033[0m\n' "$1"; }
red()   { printf '\033[31m%s\033[0m\n' "$1"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$1"; }
bold()  { printf '\033[1m%s\033[0m\n' "$1"; }

need() { command -v "$1" >/dev/null 2>&1 || { red "Missing tool: $1"; MISSING=1; }; }

echo "======================================================"
bold " Email Verifier - VPS pre-flight check"
echo "======================================================"

# --- tooling -------------------------------------------------------------
MISSING=0
need python3
need host || need dig
need nc
if [ "$MISSING" = "1" ]; then
  yellow "Install missing tools, e.g.:  sudo apt install -y python3 dnsutils netcat-openbsd"
fi

if [ ! -f "$CONFIG" ]; then
  red "Config not found: $CONFIG"
  echo "Copy the example first:  cp verifier_config.example.json verifier_config.json"
  exit 2
fi

# --- pull IPs + EHLO map out of the JSON with python ---------------------
read_json() {
  python3 - "$CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
ips = cfg.get("source_ips", [])
ehlo_map = cfg.get("ehlo_by_ip", {})
default_ehlo = cfg.get("ehlo_hostname", "")
ids = cfg.get("sender_identities", [])
print("IPS=" + " ".join(ips))
print("NIDENT=%d" % len(ids))
for ip in ips:
    print("EHLO %s %s" % (ip, ehlo_map.get(ip, default_ehlo)))
PY
}

CONF_OUT="$(read_json)" || { red "Could not parse $CONFIG"; exit 2; }
IPS="$(echo "$CONF_OUT" | sed -n 's/^IPS=//p')"
NIDENT="$(echo "$CONF_OUT" | sed -n 's/^NIDENT=//p')"

echo
bold "Config summary"
echo "  Source IPs:  $(echo $IPS | wc -w)"
echo "  Identities:  $NIDENT"
[ "$(echo $IPS | wc -w)" -ge 1 ] || { red "No source_ips in config."; FAIL=1; }

# helper: reverse-DNS lookup (host or dig)
ptr_of() {
  if command -v host >/dev/null 2>&1; then
    host "$1" 2>/dev/null | sed -n 's/.*domain name pointer //p' | sed 's/\.$//' | head -1
  else
    dig +short -x "$1" 2>/dev/null | sed 's/\.$//' | head -1
  fi
}
# helper: forward A lookup
a_of() {
  if command -v host >/dev/null 2>&1; then
    host -t A "$1" 2>/dev/null | sed -n 's/.* has address //p' | head -1
  else
    dig +short A "$1" 2>/dev/null | head -1
  fi
}

# --- 1. port 25 outbound -------------------------------------------------
echo
bold "[1/3] Port 25 outbound"
if nc -z -w 8 gmail-smtp-in.l.google.com 25 2>/dev/null; then
  green "  OK - reached Google MX on port 25. Outbound 25 is open."
else
  red   "  BLOCKED - could not reach port 25. Verification cannot work here."
  echo  "  Fix: use a provider that allows port 25 (Hetzner/OVH/Contabo) or open a ticket."
  FAIL=1
fi

# --- 2. per-IP: interface binding + fwd/rev DNS --------------------------
echo
bold "[2/3] Source IPs: binding + forward/reverse DNS"
LOCAL_IPS="$(ip -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
for ip in $IPS; do
  ehlo="$(echo "$CONF_OUT" | sed -n "s/^EHLO $ip //p")"
  echo "  --- $ip  (EHLO: ${ehlo:-<none>})"

  # bound to this host?
  if echo "$LOCAL_IPS" | grep -qx "$ip"; then
    green "      bound to a local interface"
  else
    red   "      NOT on any local interface - add it (netplan / ip addr add)"
    FAIL=1
  fi

  if [ -z "$ehlo" ] || [ "$ehlo" = "mail.example.com" ]; then
    red "      no real EHLO hostname set for this IP in config"
    FAIL=1
    continue
  fi

  ptr="$(ptr_of "$ip")"
  fwd="$(a_of "$ehlo")"

  if [ "$ptr" = "$ehlo" ]; then
    green "      PTR ok: $ip -> $ptr"
  else
    red   "      PTR mismatch: $ip -> '${ptr:-<none>}' (want $ehlo)"
    FAIL=1
  fi

  if [ "$fwd" = "$ip" ]; then
    green "      A   ok: $ehlo -> $fwd"
  else
    red   "      A mismatch: $ehlo -> '${fwd:-<none>}' (want $ip)"
    FAIL=1
  fi
done

# --- 3. python deps ------------------------------------------------------
echo
bold "[3/3] Python dependencies"
if python3 -c "import dns.resolver" 2>/dev/null; then
  green "  dnspython installed"
else
  red   "  dnspython missing -> pip install -r requirements.txt"
  FAIL=1
fi

# --- verdict -------------------------------------------------------------
echo
echo "======================================================"
if [ "$FAIL" = "0" ]; then
  green " ALL CHECKS PASSED - safe to run a live verification."
  echo  " Next:  python3 verify_emails.py --input email_verification_queue.csv \\"
  echo  "                --config $CONFIG --output results.csv"
  exit 0
else
  red " CHECKS FAILED - fix the red items above before a live run."
  echo " (You can still test logic offline: add --dry-run, no sockets opened.)"
  exit 1
fi
