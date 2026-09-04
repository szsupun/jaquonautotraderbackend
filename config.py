"""
Auto Trader Bot — Configuration.

Every value here comes from the environment (via a local `.env` file if
present, or real OS environment variables on a hosting platform — either
works identically since `load_dotenv()` just populates `os.environ`).
Nothing secret lives in this file itself; `.env` is gitignored.

See `.env.example` for the full list of variables and what each does.
"""

import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() == "true"


def _int_list_env(name: str) -> list:
    raw = os.environ.get(name, "")
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


ASSET_LOAD_WAIT_SECONDS = int(os.environ.get("ASSET_LOAD_WAIT_SECONDS", "20"))

# ============================================================================
# TELEGRAM BOT
# ============================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Telegram user ID(s) allowed to see the admin dashboard.
TELEGRAM_ADMIN_IDS = _int_list_env("TELEGRAM_ADMIN_IDS")

# ============================================================================
# DATABASE (MongoDB)
# ============================================================================
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB_NAME = os.environ.get("MONGODB_DB_NAME", "autotrader")

# ============================================================================
# MINI APP
# ============================================================================
# The frontend (deployed separately, e.g. on Vercel) opens this URL as the
# Telegram WebApp. Telegram WebApps require HTTPS — leave blank to hide the
# "Open App" button until you've deployed the frontend.
MINIAPP_URL = os.environ.get("MINIAPP_URL", "")

# This backend's own API host/port.
MINIAPP_HOST = os.environ.get("MINIAPP_HOST", "0.0.0.0")
MINIAPP_PORT = int(os.environ.get("MINIAPP_PORT", "8001"))

# Origins allowed to call this API (CORS). "*" is convenient for local dev
# but should be tightened to your real frontend domain(s) in production.
FRONTEND_ORIGINS = [
    o.strip()
    for o in os.environ.get("FRONTEND_ORIGINS", "*").split(",")
    if o.strip()
]

# ============================================================================
# MULTI-USER TRADING (many SSIDs, one backend)
# ============================================================================
# Every Telegram user who opens the Mini App gets their own isolated trading
# session (own SSID, own settings, own asyncio task) — see session_manager.py.
#
# Leave empty to let ANY Telegram user create a session. Set this to a list
# of Telegram user IDs to restrict who may use the app at all (private beta).
ALLOWED_USER_IDS = _int_list_env("ALLOWED_USER_IDS")

# User ids excluded from every admin-panel *listing* (Users, Subscriptions,
# Sessions, Leaderboard) — for accounts an admin wants full trading access
# granted to without other admins seeing that grant exists. Does not affect
# their own access at all (permissions_store gates that independently) or
# non-listing admin actions (e.g. broadcast still reaches them) — only
# what shows up in those four views.
HIDDEN_ADMIN_USER_IDS = _int_list_env("HIDDEN_ADMIN_USER_IDS")

# SECURITY: when this backend is reached through a local tunnel (ngrok,
# cloudflared, etc.), the tunnel daemon connects to this process over
# 127.0.0.1 — so EVERY request, including real ones from strangers on the
# internet, looks like it came from localhost. auth.py's dev-bypass (which
# skips Telegram verification for "local" requests, for easy testing in a
# plain browser) would otherwise silently apply to that public traffic too,
# including for admin endpoints. It only activates at all if this is
# explicitly set to true — never infer "local" from the request's IP alone.
# Leave this false/unset for any deployment reachable from the internet;
# only set DEV_BYPASS_AUTH=true when running purely on localhost with no
# tunnel active.
DEV_BYPASS_AUTH = _bool_env("DEV_BYPASS_AUTH", False)

# Hard cap on simultaneously *trading* sessions. Protects the host machine
# and your outbound IP from PocketOption rate-limiting / throttling when
# many users hit Start at once. Extra Start requests get a 429 until a slot
# frees up.
MAX_CONCURRENT_TRADERS = int(os.environ.get("MAX_CONCURRENT_TRADERS", "25"))

# How many PocketOption *connect* handshakes may be in flight at once across
# all users. Connecting is the heaviest/slowest step (~20s asset load wait);
# capping it prevents a burst of simultaneous logins from stalling everyone.
MAX_CONCURRENT_CONNECTS = int(os.environ.get("MAX_CONCURRENT_CONNECTS", "5"))

# Random 0..N second delay added before each connect attempt, so a burst of
# users starting at the same moment doesn't hit PocketOption in one spike.
CONNECT_JITTER_MAX_SECONDS = float(os.environ.get("CONNECT_JITTER_MAX_SECONDS", "4.0"))

# ============================================================================
# DEMO CONNECTIVITY RELAYS
# ============================================================================
# PocketOption's demo server (185.104.208.0/24) has been unreachable from
# this VPS's own network (Contabo) since day one — confirmed by direct,
# repeated testing, not assumed. Real-money trading is unaffected; it
# connects directly, no relay needed. DEMO_RELAYS lists WireGuard relay
# servers — each on a genuinely different network/provider, so a problem
# on one doesn't take out the rest — that carry ONLY the demo subnet's
# traffic. Every other request (Mongo, Telegram, real-money trading)
# still goes direct, unaffected by any of this.
#
# The first entry is used today. session_manager.py's connect-retry loop
# picks which entry to use on each retry (see relay_index there) — with
# one entry that's a no-op (always the same relay); once a second entry
# exists here AND its WireGuard tunnel is set up on this VPS, retries
# start actually rotating between them. Adding a relay later is meant to
# be just: bring up its WireGuard tunnel (same steps as the current one),
# add its {name, endpoint, public_key} object below — no other code
# changes required.
#
# Example:
#   DEMO_RELAYS=[{"name":"interserver-lax","endpoint":"153.75.235.168:51820","public_key":"8ji+M87BL5/1ie0b4qAEqYkAThV4nS9SWsW2e6InfXQ="}]
def _demo_relays_env() -> list:
    raw = os.environ.get("DEMO_RELAYS", "[]")
    try:
        relays = json.loads(raw)
        if isinstance(relays, list):
            return relays
    except json.JSONDecodeError:
        logging.getLogger(__name__).error(f"DEMO_RELAYS is not valid JSON, ignoring it: {raw[:200]}")
    return []


DEMO_RELAYS = _demo_relays_env()

# ============================================================================
# LOGGING
# ============================================================================
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_FILE = os.environ.get("LOG_FILE", "auto_trader.log")
