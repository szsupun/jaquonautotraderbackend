"""
Per-user completed-session archive (MongoDB-backed).

Each time a user's trading loop stops (user-initiated, risk-limit, or
error), session_manager.py archives a summary of that run here — separate
from the live in-progress trade list in RiskManager, which resets every
time Start is pressed. This is what powers the History tab's expandable
"Recent Sessions" list.

Stored as one document per user holding a capped array of session records
(most recent first) — matches the old one-file-per-user shape closely
enough that no other code needed to change.
"""

from __future__ import annotations

import logging
from typing import Dict, List

from db import get_db

logger = logging.getLogger(__name__)

_sessions = get_db()["session_history"]
MAX_SESSIONS_KEPT = 20


def load_session_history(user_id: int) -> list:
    doc = _sessions.find_one({"_id": user_id})
    if not doc:
        return []
    return doc.get("sessions", [])


def load_session_history_bulk(user_ids: List[int]) -> Dict[int, list]:
    """Same as load_session_history but one round trip for every id in
    user_ids — for the admin dashboard, which used to call this once per
    user (sometimes more than once) and fell over as the user count grew."""
    result: Dict[int, list] = {uid: [] for uid in user_ids}
    for doc in _sessions.find({"_id": {"$in": user_ids}}):
        result[doc["_id"]] = doc.get("sessions", [])
    return result


def append_session(user_id: int, record: dict) -> None:
    """Insert a newly-completed session at the front, capped to the most
    recent MAX_SESSIONS_KEPT so this document can't grow unbounded."""
    try:
        _sessions.update_one(
            {"_id": user_id},
            {
                "$push": {
                    "sessions": {
                        "$each": [record],
                        "$position": 0,
                        "$slice": MAX_SESSIONS_KEPT,
                    }
                }
            },
            upsert=True,
        )
    except Exception as e:
        logger.error(f"Failed to save session history for {user_id}: {e}")
