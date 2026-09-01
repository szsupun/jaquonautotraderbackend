"""
Per-user permissions — admin-controlled, never user-editable.

Tracks two independent subscription tracks per user, each "monthly"
(auto-expires 30 days after being granted) or "lifetime" (never expires):
  - real: real-money PocketOption trading
  - demo: demo-account auto-trading

Both are gated the same way — a user with no active grant on a track can't
use that track at all, demo included. It's deliberately separate from
TradingSettings: settings are things a user configures for themselves,
permissions are things only an admin grants. A user's own /api/settings
call can never touch this file.

Expiry is checked lazily (on every is_..._enabled() call) rather than via
a background job — there's no clock to keep running and no way for a
monthly grant to outlive its 30 days even if the backend restarts in
between.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from db import get_db

logger = logging.getLogger(__name__)

_permissions = get_db()["permissions"]
MONTHLY_DURATION_DAYS = 30

# Key names for the "real" track are kept exactly as they were before demo
# support existed, so existing granted permissions on disk keep working
# with no migration. Demo gets its own prefixed set of keys in the same file.
_TRACK_KEYS = {
    "real": {
        "enabled": "real_trading_enabled",
        "sub_type": "subscription_type",
        "expires": "subscription_expires_at",
        "granted": "granted_at",
    },
    "demo": {
        "enabled": "demo_trading_enabled",
        "sub_type": "demo_subscription_type",
        "expires": "demo_subscription_expires_at",
        "granted": "demo_granted_at",
    },
}

DEFAULT_PERMISSIONS = {
    "real_trading_enabled": False,
    "subscription_type": None,  # "monthly" | "lifetime" | None
    "subscription_expires_at": None,  # only set for "monthly"
    "granted_at": None,
    "demo_trading_enabled": False,
    "demo_subscription_type": None,
    "demo_subscription_expires_at": None,
    "demo_granted_at": None,
    "updated_at": None,
    "updated_by": None,
}


def load_permissions(user_id: int) -> dict:
    doc = _permissions.find_one({"_id": user_id})
    if not doc:
        return dict(DEFAULT_PERMISSIONS)
    doc.pop("_id", None)
    merged = dict(DEFAULT_PERMISSIONS)
    merged.update(doc)
    return merged


def load_permissions_bulk(user_ids: List[int]) -> Dict[int, dict]:
    """Same as load_permissions but one round trip for every id in
    user_ids — for the admin dashboard, which used to call load_permissions
    (twice, via _status + _is_enabled) per user per track and fell over as
    the user count grew."""
    result = {uid: dict(DEFAULT_PERMISSIONS) for uid in user_ids}
    for doc in _permissions.find({"_id": {"$in": user_ids}}):
        uid = doc.pop("_id")
        merged = dict(DEFAULT_PERMISSIONS)
        merged.update(doc)
        result[uid] = merged
    return result


def _save(user_id: int, perms: dict) -> None:
    try:
        _permissions.replace_one({"_id": user_id}, {"_id": user_id, **perms}, upsert=True)
    except Exception as e:
        logger.error(f"Failed to save permissions for {user_id}: {e}")


def _grant(user_id: int, mode: str, subscription_type: str, admin_id: int) -> dict:
    if subscription_type not in ("monthly", "lifetime"):
        raise ValueError("subscription_type must be 'monthly' or 'lifetime'")
    keys = _TRACK_KEYS[mode]
    now = datetime.now(timezone.utc)
    perms = load_permissions(user_id)
    perms[keys["enabled"]] = True
    perms[keys["sub_type"]] = subscription_type
    perms[keys["expires"]] = (
        (now + timedelta(days=MONTHLY_DURATION_DAYS)).isoformat()
        if subscription_type == "monthly"
        else None
    )
    perms[keys["granted"]] = now.isoformat()
    perms["updated_at"] = now.isoformat()
    perms["updated_by"] = admin_id
    _save(user_id, perms)
    return perms


def _revoke(user_id: int, mode: str, admin_id: int) -> dict:
    keys = _TRACK_KEYS[mode]
    perms = load_permissions(user_id)
    perms[keys["enabled"]] = False
    perms[keys["sub_type"]] = None
    perms[keys["expires"]] = None
    perms["updated_at"] = datetime.now(timezone.utc).isoformat()
    perms["updated_by"] = admin_id
    _save(user_id, perms)
    return perms


def _is_enabled_from_perms(perms: dict, mode: str) -> bool:
    keys = _TRACK_KEYS[mode]
    if not perms.get(keys["enabled"]):
        return False
    if perms.get(keys["sub_type"]) == "monthly":
        expires_at = perms.get(keys["expires"])
        if not expires_at:
            return False
        return datetime.now(timezone.utc) < datetime.fromisoformat(expires_at)
    return True  # lifetime


def _is_enabled(user_id: int, mode: str) -> bool:
    """The actual gate used everywhere trading permission matters — a
    lapsed monthly subscription returns False here automatically, no
    separate expiry sweep needed."""
    return _is_enabled_from_perms(load_permissions(user_id), mode)


def _status_from_perms(perms: dict, mode: str) -> dict:
    """Computed view for the admin dashboard: whether access is actually
    active right now (accounting for expiry), and how long is left."""
    keys = _TRACK_KEYS[mode]
    active = _is_enabled_from_perms(perms, mode)
    remaining_seconds: Optional[float] = None
    sub_type = perms.get(keys["sub_type"])
    expires_at_raw = perms.get(keys["expires"])
    if sub_type == "monthly" and expires_at_raw:
        expires_at = datetime.fromisoformat(expires_at_raw)
        remaining_seconds = max(0.0, (expires_at - datetime.now(timezone.utc)).total_seconds())
    return {
        "trading_enabled": bool(perms.get(keys["enabled"])),
        "subscription_type": sub_type,
        "subscription_expires_at": expires_at_raw,
        "granted_at": perms.get(keys["granted"]),
        "active": active,
        "remaining_seconds": remaining_seconds,
    }


def _status(user_id: int, mode: str) -> dict:
    return _status_from_perms(load_permissions(user_id), mode)


# ── Real-trading track (unchanged public API) ──────────────────────────────

def grant_real_trading(user_id: int, subscription_type: str, admin_id: int) -> dict:
    return _grant(user_id, "real", subscription_type, admin_id)


def revoke_real_trading(user_id: int, admin_id: int) -> dict:
    return _revoke(user_id, "real", admin_id)


def is_real_trading_enabled(user_id: int) -> bool:
    return _is_enabled(user_id, "real")


def subscription_status(user_id: int) -> dict:
    status = _status(user_id, "real")
    # Keep the historical field name callers/frontend already rely on.
    status["real_trading_enabled"] = status.pop("trading_enabled")
    return status


# ── Demo-trading track ──────────────────────────────────────────────────────

def grant_demo_trading(user_id: int, subscription_type: str, admin_id: int) -> dict:
    return _grant(user_id, "demo", subscription_type, admin_id)


def revoke_demo_trading(user_id: int, admin_id: int) -> dict:
    return _revoke(user_id, "demo", admin_id)


def is_demo_trading_enabled(user_id: int) -> bool:
    return _is_enabled(user_id, "demo")


def demo_subscription_status(user_id: int) -> dict:
    status = _status(user_id, "demo")
    status["demo_trading_enabled"] = status.pop("trading_enabled")
    return status


# ── Bulk views (admin dashboard) ────────────────────────────────────────────
# One query for every user instead of load_permissions() called twice per
# user per track (subscription_status + demo_subscription_status each used
# to reload it) — that's what made the admin dashboard fall over as the
# user count grew, since every one of those was a separate blocking Mongo
# round trip on the single event-loop thread.

def subscription_status_bulk(user_ids: List[int]) -> Dict[int, dict]:
    return _subscription_status_bulk_for(load_permissions_bulk(user_ids), "real")


def demo_subscription_status_bulk(user_ids: List[int]) -> Dict[int, dict]:
    return _subscription_status_bulk_for(load_permissions_bulk(user_ids), "demo")


def subscription_status_bulk_both(
    user_ids: List[int],
) -> Tuple[Dict[int, dict], Dict[int, dict]]:
    """Same result as calling subscription_status_bulk and
    demo_subscription_status_bulk separately, but a single
    load_permissions_bulk round trip instead of two identical ones — for
    callers (admin dashboard routes) that need both tracks at once."""
    perms_map = load_permissions_bulk(user_ids)
    return (
        _subscription_status_bulk_for(perms_map, "real"),
        _subscription_status_bulk_for(perms_map, "demo"),
    )


def _subscription_status_bulk_for(
    perms_map: Dict[int, dict], track: str
) -> Dict[int, dict]:
    out = {}
    for uid, perms in perms_map.items():
        s = _status_from_perms(perms, track)
        s[f"{track}_trading_enabled"] = s.pop("trading_enabled")
        out[uid] = s
    return out
