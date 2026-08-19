# WireGuard private link between the two VPSs

Puts `srv1748708` (187.127.179.167) and `srv1748712` (187.127.179.168) on a
private `10.8.0.0/24` network so the coordinator port never touches the public
internet.

| Box | Role | Public IP | WireGuard IP |
|-----|------|-----------|--------------|
| srv1748708 | coordinator | 187.127.179.167 | **10.8.0.1** |
| srv1748712 | worker | 187.127.179.168 | **10.8.0.2** |

## 1. Generate a keypair on EACH box

```bash
wg genkey | tee privatekey | wg pubkey > publickey
cat privatekey   # this box's PrivateKey
cat publickey    # this box's PublicKey - give it to the OTHER box
```

## 2. Fill in the configs

- On **vps1 (708)**: use `deploy/wireguard-vps1.conf`. Put **vps1's** private key in
  `PrivateKey`, and **vps2's** public key in the `[Peer] PublicKey`.
- On **vps2 (712)**: use `deploy/wireguard-vps2.conf`. Put **vps2's** private key in
  `PrivateKey`, and **vps1's** public key in the `[Peer] PublicKey`.

Install each as `/etc/wireguard/wg0.conf`:

```bash
sudo cp deploy/wireguard-vpsN.conf /etc/wireguard/wg0.conf   # N = this box
sudo nano /etc/wireguard/wg0.conf                            # paste the two keys
```

## 3. Open the WireGuard port, bring the tunnel up (both boxes)

```bash
sudo ufw allow 51820/udp        # WireGuard handshake port (public)
sudo wg-quick up wg0
sudo systemctl enable wg-quick@wg0   # survive reboot
```

## 4. Verify the tunnel

```bash
# from vps2, ping the coordinator's private IP:
ping -c3 10.8.0.1
sudo wg show                    # should list the peer with a recent handshake
```

## 5. Lock the coordinator port to the tunnel only

The coordinator listens on `10.8.0.1:8900`. Make sure it is **not** reachable
publicly:

```bash
# on vps1 (coordinator): allow 8900 only over the wg0 interface
sudo ufw allow in on wg0 to any port 8900 proto tcp
sudo ufw deny 8900/tcp          # block it everywhere else
```

Now start the coordinator bound to the private IP and point both workers at
`http://10.8.0.1:8900` — see `deploy/COORDINATOR_SETUP.md`.
