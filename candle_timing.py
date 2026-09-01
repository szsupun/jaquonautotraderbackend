"""
Candle-boundary alignment.

Binary options expiries run on a fixed grid (every 5s, every 60s, etc.)
regardless of when a bot happens to finish its analysis. Entering mid-candle
means the trade's expiry doesn't line up with the candle the strategy just
read its indicators off of. These helpers compute how long to wait so a
trade is placed right as the next candle of the chosen duration opens.

Unix epoch time already sits on whole-minute/whole-second boundaries, so
`epoch % duration` gives wall-clock alignment for free for any duration
that divides evenly into 60s/3600s (5s, 15s, 30s, 60s, 120s, 300s, ...).
`tz_offset_hours` only changes anything once duration spans an hour
boundary — it's accepted for correctness but is a no-op for the short
expiries (5s-5m) these bots actually trade.
"""
from __future__ import annotations

import time

# Below this many seconds of drift, treat "now" as already on the boundary
# rather than waiting almost a full extra candle for the next one.
_ON_BOUNDARY_TOLERANCE = 0.2


def seconds_until_next_candle(duration_seconds: float, tz_offset_hours: float = 0.0) -> float:
    if duration_seconds <= 0:
        return 0.0
    now = time.time() + tz_offset_hours * 3600
    remainder = now % duration_seconds
    if remainder <= _ON_BOUNDARY_TOLERANCE:
        return 0.0
    return duration_seconds - remainder
