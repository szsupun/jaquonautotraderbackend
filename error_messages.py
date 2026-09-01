"""
Turns raw exceptions from the trading loop into short, reassuring text a
non-technical customer actually reads (Mini App Terminal tab, /api/status).

The exact exception (type + message) is always logged and kept in
UserSession.last_error_detail for the admin panel — this only controls
what the customer sees for the handful of *unexpected* errors that reach
the trading loop's catch-all. Known, expected failures (no SSID, PocketOption
unreachable, risk limits hit) already have their own hand-written messages
elsewhere in session_manager.py and never pass through here.
"""

from __future__ import annotations


def humanize_error(e: Exception) -> str:
    text = str(e).lower()
    name = type(e).__name__.lower()

    if "timeout" in text or "timeout" in name:
        return "Connection to PocketOption is slow right now — retrying automatically."
    if any(k in text for k in ("connection", "closed", "reset", "refused", "unreachable", "network", "socket")):
        return "Lost connection to PocketOption — retrying automatically."
    if any(k in text for k in ("balance", "insufficient", "funds")):
        return "Couldn't read your account balance — retrying automatically."
    if any(k in text for k in ("ssid", "unauthorized", "auth", "expired session")):
        return "Your PocketOption session looks expired — please re-paste your SSID."

    return "A temporary issue occurred on our end — your funds and settings are safe. Retrying automatically."
