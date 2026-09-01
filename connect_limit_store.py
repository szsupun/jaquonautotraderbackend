"""
Per-user daily trading-session limiter (MongoDB-backed).

Counts user-initiated session starts (one Start button press = one
session), matching the "N sessions per day" allowance shown to users —
NOT raw PocketOption connect attempts. Those are a different concern:
within a single session, trader.py/session_manager.py already cap how
many times a flaky connection gets retried before giving up (see
_run_loop's consecutive_stale_balance handling) — that's what protects
the account from looking like abuse to PocketOption's own throttling.
Conflating the two here meant a single flaky Start could silently burn
2-3 "sessions" off the daily count for one button press, which is a
mismatch with what's actually promised to users. Persisted so the count
survives a bot restart rather than resetting for free.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Tuple

from pymongo import ReturnDocument

from db import get_db

logger = logging.getLogger(__name__)

_connect_limits = get_db()["connect_limits"]

DEFAULT_DAILY_LIMIT = 25


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def check_and_record_session_start(
    user_id: int, daily_limit: int = DEFAULT_DAILY_LIMIT
) -> Tuple[bool, str]:
    """
    Atomically record one session start for today and check it against the
    daily limit.

    Returns (allowed, reason). Call this once per user-initiated Start
    (not on internal reconnects) — if not allowed, refuse to start the
    session at all.
    """
    today = _today()
    key = f"{user_id}:{today}"
    try:
        doc = _connect_limits.find_one_and_update(
            {"_id": key},
            {"$inc": {"count": 1}, "$setOnInsert": {"user_id": user_id, "date": today}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        count = doc["count"]
    except Exception as e:
        # Rate-limit bookkeeping failing shouldn't itself block trading —
        # fail open.
        logger.error(f"Session-limit check failed for {user_id}: {e}")
        return True, "OK"

    if count > daily_limit:
        return False, (
            f"Daily session limit reached ({daily_limit}/day) — resets "
            f"at midnight UTC."
        )
    return True, "OK"


def get_connect_usage_today(
    user_id: int, daily_limit: int = DEFAULT_DAILY_LIMIT
) -> Tuple[int, int]:
    """
    Read-only — how many sessions this user has started today and the
    limit, for showing "N/25 left" in the UI. Doesn't record a session.
    """
    doc = _connect_limits.find_one({"_id": f"{user_id}:{_today()}"})
    used = doc["count"] if doc else 0
    return used, daily_limit
