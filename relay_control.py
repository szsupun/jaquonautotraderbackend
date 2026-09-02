"""
Picks which demo-connectivity relay a connect retry should use — see
DEMO_RELAYS in config.py for why this exists (PocketOption's demo server
is unreachable directly from this VPS).

Split into two pieces on purpose:
  - select_demo_relay(): pure selection logic, fully correct and testable
    today even with only one relay configured.
  - activate_demo_relay(): the actual network-level switch (which
    WireGuard tunnel carries 185.104.208.0/24 right now). With a single
    relay there is nothing to switch between, so this is correctly a
    no-op — that tunnel is already the only route. Implementing the real
    multi-relay switch (bringing up a second WireGuard interface and
    flipping the active route between them) is deferred until a second
    relay actually exists: it needs a real second tunnel to build and
    verify against, the same way the first relay was verified tonight
    rather than assumed to work.

Selection is priority-based, not round-robin: DEMO_RELAYS[0] (the relay
that's actually been verified reliable — see config.py) gets every
attempt except the very last one in a retry sequence, which falls back
to DEMO_RELAYS[1] if a second relay exists. That second relay is meant
for entries with a real but lower success rate (per the operator's own
long-run experience, not a proven-broken one) — a genuine last resort
tried only once the primary has already failed the rest of the
sequence, never touched at all while the primary is healthy.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

from config import DEMO_RELAYS

logger = logging.getLogger(__name__)

_DEMO_SUBNET = "185.104.208.0/24"
_active_interface: Optional[str] = None


def select_demo_relay(attempt_index: int, max_attempts: int) -> Optional[dict]:
    """attempt_index: 0 on the first connect attempt of a retry sequence,
    incrementing on each automatic retry (see consecutive_connect_failures
    in session_manager.py). max_attempts: the retry sequence's cap (see
    MAX_CONNECT_RETRIES in session_manager.py) — only the last attempt in
    the sequence reaches for a backup relay. Returns the relay dict to
    use, or None if no relay is configured at all."""
    if not DEMO_RELAYS:
        return None
    is_last_attempt = attempt_index >= max_attempts - 1
    if is_last_attempt and len(DEMO_RELAYS) > 1:
        return DEMO_RELAYS[1]
    return DEMO_RELAYS[0]


def activate_demo_relay(relay: dict) -> None:
    """Make `relay` the one actually carrying demo-subnet traffic, by
    pointing the kernel route at its WireGuard interface (each relay's
    tunnel stays up permanently with `Table = off` in its wg-quick
    config, so this is just picking which one the route uses — never a
    handshake delay). Every session on this box shares one route, so
    this only actually issues a change when the target differs from
    what's already active; a session already connected and running is
    unaffected either way (Rust the library holds its own live socket,
    changing the route doesn't touch existing connections, only future
    ones). Silently no-ops if the relay has no interface configured
    (e.g. the single-relay case) or the command fails — a connect
    attempt through whatever's currently active is still better than
    raising here."""
    interface = relay.get("interface")
    if not interface:
        return
    global _active_interface
    if interface == _active_interface:
        return
    try:
        subprocess.run(
            ["ip", "route", "replace", _DEMO_SUBNET, "dev", interface],
            check=True, timeout=5, capture_output=True,
        )
        _active_interface = interface
        logger.info(f"Demo relay switched to {relay.get('name', interface)}")
    except Exception as e:
        logger.error(f"Failed to switch demo relay to {interface}: {e}")
