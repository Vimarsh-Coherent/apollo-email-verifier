# VPS Setup — do these in order

The code is done. The only things left are **infrastructure**: port 25 and DNS.
Get those two right and verification works. This is the exact order.

---

## Step 0 — Choose a provider that allows port 25 outbound

Most big clouds **permanently block** port 25 outbound. Do not use them:

| Provider | Port 25 | Use it? |
|---|---|---|
| AWS / Google Cloud / Azure | Blocked | ❌ |
| DigitalOcean / Vultr | Blocked by default | ⚠️ ticket, often denied |
| **Hetzner** | Open (after account age) | ✅ recommended |
| **OVH / SoYouStart** | Usually open | ✅ recommended |
| **Contabo** | Usually open | ✅ cheap extra IPs |
| **Scaleway** | Open | ✅ |

Buy **one VPS** and add **5 IPs** to it (Hetzner: "Additional IPs"; OVH: "Failover IPs").
2 GB RAM is plenty — this is network-bound, not CPU-bound.

---

## Step 1 — First login, test port 25 immediately

Before doing anything else, confirm outbound 25 works. If it doesn't, stop —
nothing else matters until it's fixed.

```bash
sudo apt update && sudo apt install -y netcat-openbsd dnsutils python3 python3-venv python3-pip git
nc -zv -w8 gmail-smtp-in.l.google.com 25
```
`succeeded` / `open` → good. Hangs or `timed out` → port 25 is blocked; open a
provider ticket or switch providers.

---

## Step 2 — Attach the 5 IPs

Find your interface: `ip -o link show` (e.g. `eth0`). Then:

```bash
sudo cp deploy/netplan-extra-ips.example.yaml /etc/netplan/60-extra-ips.yaml
sudo nano /etc/netplan/60-extra-ips.yaml     # real interface name + your 5 IPs
sudo netplan apply
ip a                                          # all 5 IPs show on the interface
curl --interface 203.0.113.11 ifconfig.me     # prints 203.0.113.11 — repeat for each
```

---

## Step 3 — DNS (the make-or-break step)

Pick a domain you control, e.g. `verify.yourdomain.com`, and 5 EHLO hostnames
`mx1..mx5.verify.yourdomain.com`. You need three record types.

**a) Forward A records** — at your DNS host (Cloudflare, etc.):
```
mx1.verify.yourdomain.com  A  203.0.113.11
mx2.verify.yourdomain.com  A  203.0.113.12
mx3.verify.yourdomain.com  A  203.0.113.13
mx4.verify.yourdomain.com  A  203.0.113.14
mx5.verify.yourdomain.com  A  203.0.113.15
```

**b) Reverse PTR records** — at your **VPS provider's** panel (rDNS is set by
whoever owns the IP, NOT your DNS host). For each IP, set the PTR to the
matching hostname:
```
203.0.113.11 -> mx1.verify.yourdomain.com
...
203.0.113.15 -> mx5.verify.yourdomain.com
```
The PTR must point back to the same name the A record points from. This
forward/reverse match is what Gmail/Outlook/Mimecast check before they'll talk.

**c) SPF** — one TXT record on the sender domain:
```
verify.yourdomain.com  TXT  "v=spf1 ip4:203.0.113.11 ip4:203.0.113.12 ip4:203.0.113.13 ip4:203.0.113.14 ip4:203.0.113.15 -all"
```

DNS can take minutes to a few hours to propagate.

---

## Step 4 — Get the code + install

```bash
git clone <your-repo> && cd scraper_-main      # or scp the folder up
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

---

## Step 5 — Fill in the config

```bash
cp verifier_config.example.json verifier_config.json
nano verifier_config.json
```
Set `source_ips` (your 5), `sender_identities` (10 addresses on your sender
domain), and `ehlo_by_ip` (each IP → its `mxN` hostname, matching Step 3).

---

## Step 6 — Run the pre-flight check

This script tests port 25 + every IP's forward/reverse DNS automatically:

```bash
chmod +x deploy/check_setup.sh
./deploy/check_setup.sh
```
Fix every red line before continuing. Green across the board = ready.

---

## Step 7 — Test in order, then go live

```bash
# a) Offline logic check — no sockets:
python verify_emails.py --input email_verification_queue.csv \
    --config verifier_config.json --output test.csv --dry-run

# b) Small live test — 5-10 addresses you KNOW are real. Include some where
#    is_known_email=true as a control: if those come back undeliverable,
#    the setup is wrong, not the address.
python verify_emails.py --input small_test.csv \
    --config verifier_config.json --output small_results.csv

# c) Full run (resumable — Ctrl-C and re-run to continue):
python verify_emails.py --input email_verification_queue.csv \
    --config verifier_config.json --output results.csv
```

For long runs, detach with tmux (`tmux new -s verify`, Ctrl-B then D) or install
the systemd service (`deploy/verifier.service`).

---

## When results look wrong — check these three, in order

1. **Everything comes back `unknown`** → port 25 is blocked (redo Step 1) or the
   whole run never opened a socket.
2. **Only Gmail/Outlook come back `unknown`, smaller domains work** → rDNS/PTR
   mismatch (redo Step 3b, verify with `./deploy/check_setup.sh`).
3. **Intermittent tempfails / slow** → fresh IPs with no reputation getting
   greylisted. Expected early. Keep the conservative rate limits; warm up slowly.
