"""
Per-user Telegram profile cache (MongoDB-backed) — real name/username, so
the admin dashboard can show "Jaquon (@jaquon)" instead of a bare numeric ID.

Populated automatically the first time each user makes an authenticated
request each process run (see auth.py) — cheap, since it only writes once
per user per backend restart, not on every poll.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from db import get_db

logger = logging.getLogger(__name__)

_profiles = get_db()["profiles"]


def load_profile(user_id: int) -> Optional[dict]:
    doc = _profiles.find_one({"_id": user_id})
    if not doc:
        return None
    doc.pop("_id", None)
    return doc


def save_profile(user_id: int, telegram_user: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    existing = load_profile(user_id) or {}
    existing.update({
        "first_name": telegram_user.get("first_name"),
        "last_name": telegram_user.get("last_name"),
        "username": telegram_user.get("username"),
        "is_premium": bool(telegram_user.get("is_premium")),
        "last_seen": now,
    })
    existing.setdefault("first_seen", now)
    try:
        _profiles.replace_one({"_id": user_id}, {"_id": user_id, **existing}, upsert=True)
    except Exception as e:
        logger.error(f"Failed to save profile for {user_id}: {e}")


def _format_display_name(user_id: int, p: Optional[dict]) -> str:
    if not p:
        return f"User {user_id}"
    name = " ".join(filter(None, [p.get("first_name"), p.get("last_name")])).strip()
    if name and p.get("username"):
        return f"{name} (@{p['username']})"
    if name:
        return name
    if p.get("username"):
        return "@" + p["username"]
    return f"User {user_id}"


def display_name(user_id: int) -> str:
    return _format_display_name(user_id, load_profile(user_id))


# ── Bulk views (admin dashboard) ────────────────────────────────────────────
# One query for every user instead of load_profile() called once per user
# (directly, and again via display_name()) — see permissions_store.py's
# bulk functions for why that mattered.

def load_profiles_bulk(user_ids: List[int]) -> Dict[int, dict]:
    result: Dict[int, dict] = {}
    for doc in _profiles.find({"_id": {"$in": user_ids}}):
        uid = doc.pop("_id")
        result[uid] = doc
    return result


def display_names_bulk(user_ids: List[int]) -> Dict[int, str]:
    profiles = load_profiles_bulk(user_ids)
    return {uid: _format_display_name(uid, profiles.get(uid)) for uid in user_ids}
