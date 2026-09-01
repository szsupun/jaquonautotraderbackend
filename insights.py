"""
Aggregate trading-pattern insights across every user's archived trades —
which pairs, hours, days, and martingale steps have actually performed
best on this platform so far.

This is descriptive, not predictive: a summary of what already happened,
gated behind a minimum sample size so a pair with 5 trades can't show a
"90% win rate" that's really just noise. It is not a signal or a
guarantee — a pattern in past trades is not a promise about the next one,
and PocketOption's own pricing can drift over time.

Demo and real trades are combined: both trade the same PocketOption OTC
price feed, so which pair/hour performs best is a property of that feed,
not of which account type placed the trade. Splitting them would only
shrink an already-thin sample for no statistical benefit.

Archived sessions only (same as the admin equity curve) — a session still
in progress gets folded in once it stops, which keeps this in line with
how every other platform-wide aggregate in this app already works.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, List

MIN_SAMPLE_SIZE = 20

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _win_rate(wins: int, losses: int) -> float:
    total = wins + losses
    if total == 0:
        return 0.0
    return round(wins / total * 100.0, 1)


def _bucket_stats(bucket_trades: Dict) -> Dict:
    out = {}
    for key, trades in bucket_trades.items():
        wins = sum(1 for t in trades if t["result"] == "win")
        losses = sum(1 for t in trades if t["result"] == "loss")
        out[key] = {
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": _win_rate(wins, losses),
            "sufficient_data": len(trades) >= MIN_SAMPLE_SIZE,
        }
    return out


def _empty_bucket() -> Dict:
    return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "sufficient_data": False}


def compute_insights(all_trades: List[dict]) -> dict:
    """all_trades: flat list of trade dicts from every archived session,
    each shaped like session_manager.py's append_session() trade records
    (asset, result, martingale_step, timestamp as ISO string, ...)."""
    by_asset_trades = defaultdict(list)
    by_hour_trades = defaultdict(list)
    by_day_trades = defaultdict(list)
    by_step_trades = defaultdict(list)

    for t in all_trades:
        # doji/error don't resolve win or loss — excluded from every
        # bucket so "trades" consistently means "decisive trades" and
        # win_rate math never needs a separate denominator per view.
        if t.get("result") not in ("win", "loss"):
            continue
        ts = t["timestamp"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        by_asset_trades[t["asset"]].append(t)
        by_hour_trades[ts.hour].append(t)
        by_day_trades[_DAY_NAMES[ts.weekday()]].append(t)
        by_step_trades[t.get("martingale_step", 1)].append(t)

    by_asset = _bucket_stats(by_asset_trades)
    by_hour = _bucket_stats(by_hour_trades)
    by_day = _bucket_stats(by_day_trades)
    by_step = _bucket_stats(by_step_trades)

    asset_rows = sorted(
        [{"asset": k, **v} for k, v in by_asset.items()],
        key=lambda r: (not r["sufficient_data"], -r["win_rate"], -r["trades"]),
    )
    hour_rows = [{"hour": h, **by_hour.get(h, _empty_bucket())} for h in range(24)]
    day_rows = [{"day": d, **by_day.get(d, _empty_bucket())} for d in _DAY_NAMES]
    step_rows = sorted(
        [{"step": k, **v} for k, v in by_step.items()],
        key=lambda r: r["step"],
    )

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "total_trades": sum(v["trades"] for v in by_asset.values()),
        "min_sample_size": MIN_SAMPLE_SIZE,
        "by_asset": asset_rows,
        "by_hour_utc": hour_rows,
        "by_day_of_week": day_rows,
        "by_martingale_step": step_rows,
    }
