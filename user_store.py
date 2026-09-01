"""
Per-user settings persistence (MongoDB-backed).

Each Telegram user who opens the Mini App gets their own TradingSettings,
stored as one document per user in the `users` collection, keyed by their
Telegram user id. One document per user means concurrent users never
contend for the same record — a big part of what keeps many simultaneous
SSIDs from stepping on each other.
"""

from __future__ import annotations

from typing import Dict, List

from db import get_db
from settings import TradingSettings

_users = get_db()["users"]


def load_user_settings(user_id: int) -> TradingSettings:
    doc = _users.find_one({"_id": user_id})
    if not doc:
        settings = TradingSettings()
        save_user_settings(user_id, settings)
        return settings
    return TradingSettings.from_dict(doc)


def save_user_settings(user_id: int, settings: TradingSettings) -> None:
    _users.replace_one({"_id": user_id}, {"_id": user_id, **settings.to_dict()}, upsert=True)


def list_user_ids() -> List[int]:
    return [doc["_id"] for doc in _users.find({}, {"_id": 1})]


def load_user_settings_bulk(user_ids: List[int]) -> Dict[int, TradingSettings]:
    """Same as load_user_settings but one round trip for every id in
    user_ids — for the admin dashboard. Every id here came from
    list_user_ids() (this same collection), so a doc always exists; no
    default-and-save fallback needed the way the single-user loader has."""
    result: Dict[int, TradingSettings] = {}
    for doc in _users.find({"_id": {"$in": user_ids}}):
        uid = doc.pop("_id")
        result[uid] = TradingSettings.from_dict(doc)
    return result
