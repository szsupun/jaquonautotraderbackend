"""
Picks which demo-connectivity relay a connect retry should use — see
DEMO_RELAYS in config.py for why this exists (PocketOption's demo server
is unreachable directly from this VPS).

Split into two pieces on purpose:
  - select_demo_relay(): pure selection logic, fully correct and testable
    regardless of how many relays are configured.
  - activate_demo_relay(): the actual network-level switch (which
    WireGuard tunnel carries 185.104.208.0/24 right now).

Selection is round-robin, not priority-based. Earlier this only had two
relays and picked a "reliable primary" every time except the very last
attempt. That assumption stopped holding: real testing on 2026-09-02/03
showed every relay tried so far (InterServer, a Contabo box, a Spaceship
box) has independent good and bad stretches — at one point both of the
first two relays were down at the same moment while the third worked
fine. With no relay provably better than the others, trying a different
one on each attempt maximizes the chance that at least one of them is
healthy within the retry budget, rather than betting the first two
attempts on a single "preferred" option that might be having a bad
moment right when it's needed.
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
    in session_manager.py). max_attempts: unused now (kept in the
    signature so callers don't need to change) — every attempt just
    round-robins to the next configured relay. Returns the relay dict to
    use, or None if no relay is configured at all."""
    if not DEMO_RELAYS:
        return None
    return DEMO_RELAYS[attempt_index % len(DEMO_RELAYS)]


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
