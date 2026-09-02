# Demo connectivity relays

## Why this exists

PocketOption's **demo** server (`demo-api-eu.po.market`, `185.104.208.5`) is
unreliable to reach directly from this VPS's network (Contabo, AS51167) —
confirmed by repeated direct testing on 2026-09-02, not assumed. **Real-money
trading is unaffected** — both real-money regions (`api-eu.po.market`,
`api-us-south.po.market`) connect fine directly from this VPS, no relay
involved, ever. This whole setup exists solely to get demo-mode traffic to
`185.104.208.0/24` out through a network that can actually reach it.

Key finding from that day's testing: Contabo's reachability to the demo
server is **intermittent, not a permanent block** — it went from 0/3 to 3/3
to 0/3 successes within about 20 minutes of testing. Treat any single test
(good or bad) as a snapshot, not a verdict — that's why relays are verified
with multiple repeated tests, not one.

## Current setup (as of 2026-09-02)

Two relays, both WireGuard tunnels from the primary VPS, each carrying
*only* `185.104.208.0/24` — nothing else (Mongo, Telegram, real-money
trading) touches them:

| Priority | Name | Box | Interface | Role |
|---|---|---|---|---|
| 1 (primary) | `interserver-lax` | InterServer VPS, LA | `wg0` | Used on every normal attempt |
| 2 (last resort) | `contabo-relay` | Contabo VPS (169.58.195.60) | `wg1` | Only the *final* retry attempt before giving up |

Configured via `DEMO_RELAYS` in `.env` (JSON list — see the comment above
`DEMO_RELAYS` in `config.py` for the exact format). Selection logic lives in
`relay_control.py`; it's called from the connect-retry loop in
`session_manager.py`.

**Why last-resort, not round-robin:** with only 3 retry attempts total, a
relay with a real chance of failing (like Contabo, per the finding above)
should never eat one of the two "real" attempts on a healthy primary — it
would just waste ~90s on a coin-flip when the primary was probably fine.
Reaching for it only on the last attempt means it's pure upside: it can
only rescue a session that would've failed anyway.

**Why two WireGuard interfaces instead of reconfiguring one:** both tunnels
stay up permanently (`Table = off` in each wg-quick config, so neither
auto-adds a route). `relay_control.py` just flips which interface owns the
`185.104.208.0/24` kernel route via `ip route replace ... dev <interface>`
— instant, no handshake delay, and both tunnels' health can be checked
independently at any time.

**Known limitation:** the route is one shared, system-wide setting — not
per-user. If it's ever pointed at the fallback for one user's last attempt
while other users are mid-connect, they briefly share that path too. Not
believed to be a real problem in practice (a switch only happens when the
primary has already failed twice for *someone*, which is rare while
InterServer is healthy), but worth knowing if connection behavior ever
looks correlated across unrelated users.

## Adding a new relay (runbook)

1. **Get a VPS on a genuinely different network** than every relay already
   in the list (different company, not just a different data center of the
   same one — e.g. don't add a second Contabo box expecting independence
   from the first).

2. **On the new relay box** — install WireGuard, generate a keypair, set it
   up as a WireGuard *server*:
   ```
   apt-get install -y wireguard iptables
   wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey
   ```
   `/etc/wireguard/wg0.conf`:
   ```
   [Interface]
   Address = 10.9X.0.1/24        # pick an unused /24, e.g. 10.97.0.0/24 for the 3rd relay
   PrivateKey = <its privatekey>
   ListenPort = 51820
   PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -A FORWARD -o wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
   PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -D FORWARD -o wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

   [Peer]
   PublicKey = dDSoo7J4dmjdQWMoqQrYm/5Ry10HyTH3PyKQwAKppCI=   # primary VPS's wg0 identity — same on every relay
   AllowedIPs = 10.9X.0.2/32
   ```
   Enable IP forwarding (`echo net.ipv4.ip_forward=1 >> /etc/sysctl.conf; sysctl -p`),
   then `systemctl enable --now wg-quick@wg0`.

3. **On the primary VPS (157.173.117.59)** — new tunnel interface (`wg2`,
   `wg3`, ...), `Table = off`, pointed at the new relay:
   ```
   wg genkey | tee /tmp/priv | wg pubkey > /tmp/pub
   ```
   `/etc/wireguard/wgN.conf`:
   ```
   [Interface]
   Address = 10.9X.0.2/24
   PrivateKey = <primary's new privatekey for this tunnel>
   Table = off

   [Peer]
   PublicKey = <new relay's publickey from step 2>
   Endpoint = <new relay IP>:51820
   AllowedIPs = 185.104.208.0/24
   PersistentKeepalive = 25
   ```
   `systemctl enable --now wg-quick@wgN`, then confirm a handshake with `wg show`.

4. **Verify BEFORE trusting it** — the whole point of tonight's work was not
   assuming a relay works. From the primary VPS, with the route pointed at
   the new interface:
   ```
   ip route replace 185.104.208.0/24 dev wgN
   curl -s -o /dev/null -w "%{time_connect}s %{time_total}s %{http_code}\n" --max-time 12 https://demo-api-eu.po.market/
   ```
   Run it several times, ideally spread over a few minutes — one success
   doesn't prove it, one failure doesn't disprove it either (see the
   intermittency note above). Then put the route back:
   ```
   ip route replace 185.104.208.0/24 dev wg0
   ```

5. **Add it to `DEMO_RELAYS`** in `/root/jaquonautotrader/.env` — append a
   `{"name", "endpoint", "public_key", "interface"}` object to the JSON
   list (position in the list = priority; only the *last* one is used as
   last-resort, so a 3rd relay would currently need `relay_control.py`'s
   logic extended if you want a "try relay 2, then relay 3" chain rather
   than jumping straight past relay 2).

6. **Restart and verify end-to-end** — `systemctl restart jaquonautotrader`,
   check `journalctl -u jaquonautotrader` for a clean start, then a real
   Start/Stop cycle through the API to confirm nothing broke.

## What to tell Claude, in a future conversation

> "I've got a new VPS for a demo relay — IP `<x>`, root password `<y>`,
> hosted on `<provider>`. Add it as relay #`<N>` following RELAYS.md in the
> backend repo."

That's enough — this file has the rest.

## Lesson learned (2026-09-02)

Mid-build, `select_demo_relay()`'s signature changed but the file that
calls it (`session_manager.py`) wasn't redeployed in the same step —
broke live Start for a few minutes until caught via logs. **When a
function signature changes, redeploy every file that calls it in the same
step, not just the file the change was made in.**
