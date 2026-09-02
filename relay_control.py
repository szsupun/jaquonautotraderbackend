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
"""

from __future__ import annotations

import logging
from typing import Optional

from config import DEMO_RELAYS

logger = logging.getLogger(__name__)


def select_demo_relay(attempt_index: int) -> Optional[dict]:
    """attempt_index: 0 on the first connect attempt of a retry sequence,
    incrementing on each automatic retry (see consecutive_connect_failures
    in session_manager.py). Returns the relay dict to use, or None if no
    relay is configured at all."""
    if not DEMO_RELAYS:
        return None
    return DEMO_RELAYS[attempt_index % len(DEMO_RELAYS)]


def activate_demo_relay(relay: dict) -> None:
    """Make `relay` the one actually carrying 185.104.208.0/24 traffic.
    No-op today (single relay, already the only route). Once a second
    relay is added to DEMO_RELAYS and its own WireGuard tunnel is brought
    up on this VPS, this becomes an `ip route replace 185.104.208.0/24
    dev <that relay's interface>` call."""
    return
