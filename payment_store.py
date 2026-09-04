"""
Per-user payment tracking (MongoDB-backed) — separate from
permissions_store.py on purpose: permissions are "can this user trade,"
payments are "did the money for that actually arrive." A partner sells
this bot and settles up weekly, not per-signup — so it's normal and
expected for a freshly-granted user to sit as "not paid yet" for days
until that settlement happens. This isn't a red flag by itself, just a
number to reconcile against the weekly payout.

One record per user, holding the most recent grant's payment info —
overwritten each time an admin grants access again (e.g. a renewal).
Not a full ledger/payment history; add one later if that's ever needed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from db import get_db

logger = logging.getLogger(__name__)

_payments = get_db()["payments"]

DEFAULT_PAYMENT = {
    "amount": 0.0,
    "paid": False,
    "updated_at": None,
    "updated_by": None,
}


def load_payment(user_id: int) -> dict:
    doc = _payments.find_one({"_id": user_id})
    if not doc:
        return dict(DEFAULT_PAYMENT)
    doc.pop("_id", None)
    merged = dict(DEFAULT_PAYMENT)
    merged.update(doc)
    return merged


def load_payments_bulk(user_ids: List[int]) -> Dict[int, dict]:
    result = {uid: dict(DEFAULT_PAYMENT) for uid in user_ids}
    for doc in _payments.find({"_id": {"$in": user_ids}}):
        uid = doc.pop("_id")
        merged = dict(DEFAULT_PAYMENT)
        merged.update(doc)
        result[uid] = merged
    return result


def set_payment(user_id: int, amount: float, paid: bool, admin_id: int) -> dict:
    record = {
        "amount": amount,
        "paid": paid,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": admin_id,
    }
    try:
        _payments.replace_one({"_id": user_id}, {"_id": user_id, **record}, upsert=True)
    except Exception as e:
        logger.error(f"Failed to save payment for {user_id}: {e}")
    return record


def payment_totals(user_ids: List[int]) -> dict:
    """Aggregate for the admin overview: how much has actually come in vs.
    how much is sitting with the partner waiting on the weekly settlement."""
    payments = load_payments_bulk(user_ids)
    total_paid = 0.0
    pending_amount = 0.0
    not_paid_count = 0
    for uid in user_ids:
        p = payments[uid]
        if p["amount"] <= 0 and p["updated_at"] is None:
            continue  # never recorded — not part of either bucket
        if p["paid"]:
            total_paid += p["amount"]
        else:
            pending_amount += p["amount"]
            not_paid_count += 1
    return {
        "total_paid": round(total_paid, 2),
        "pending_amount": round(pending_amount, 2),
        "not_paid_count": not_paid_count,
    }
