"""
Trading Strategy — Signal generator.

Trend-follow system: a fast/slow moving-average crossover (see
_trend_signal) is the primary driver of CALL/PUT — short-duration binary
trades reward riding the trend that's already in motion, not waiting for
a committee of indicators to unanimously agree. RSI, candle patterns,
momentum, and support/resistance never block or reverse the trend read;
they only add to the logged confidence when they happen to agree, for
transparency into how strong a given signal actually is.

The only thing that skips a cycle is a genuinely flat/tied market (no
usable trend at all) or not enough candle history to compute one.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Minimum candles before attempting real analysis at all. Several
# indicators (S/R, momentum, trend) need 15-30 candles to mean anything;
# computing them on 5 candles was producing signals with almost no basis.
MIN_CANDLES = 20


class TradingStrategy:
    """Determines trade direction using technical analysis."""

    async def get_direction(
        self,
        asset: str,
        api,  # PocketOptionAsync instance
        mode: str = "AUTO",
    ) -> Optional[str]:
        """
        Get trade direction based on technical analysis.

        Returns:
            "CALL", "PUT", or None if there's no confident signal this cycle.
        """
        direction, _detail, _is_fallback = await self._analyze(asset, api)
        return direction

    async def get_direction_verbose(
        self,
        asset: str,
        api,
        mode: str = "AUTO",
    ) -> Tuple[Optional[str], str, bool]:
        """Same as get_direction, but also returns a human-readable summary
        of what the indicators actually computed — for surfacing real
        analysis detail in the Mini App's live terminal — plus whether this
        was a genuine indicator read or a no-data/low-confidence skip, so
        callers can choose not to surface that text as if it were a trade
        signal."""
        return await self._analyze(asset, api)

    async def _analyze(self, asset: str, api) -> Tuple[Optional[str], str, bool]:
        """
        Multi-indicator analysis combining RSI, candle patterns, momentum,
        S/R, and trend. Returns None when there isn't enough data or the
        indicators don't have real confluence — see MIN_CANDLES/MIN_CONFIDENCE.
        """
        try:
            import asyncio

            candles = await asyncio.wait_for(
                api.candles(asset, 60),  # 1-minute candles
                timeout=15.0,
            )

            if not candles or len(candles) < MIN_CANDLES:
                logger.warning(
                    f"Not enough candle data for {asset} "
                    f"({len(candles) if candles else 0} candles, need "
                    f"{MIN_CANDLES}) — skipping this cycle rather than "
                    f"guessing."
                )
                return None, "Not enough market data yet — skipping this cycle", True

            # Sort by timestamp
            sorted_candles = sorted(
                candles,
                key=lambda c: c.get("timestamp", c.get("time", 0)),
            )

            # Extract OHLC data
            closes = []
            opens = []
            highs = []
            lows = []
            for c in sorted_candles:
                try:
                    closes.append(float(c.get("close", 0)))
                    opens.append(float(c.get("open", 0)))
                    highs.append(float(c.get("high", c.get("close", 0))))
                    lows.append(float(c.get("low", c.get("close", 0))))
                except (TypeError, ValueError):
                    continue

            if len(closes) < MIN_CANDLES:
                return None, "Not enough usable market data — skipping this cycle", True

            # ── Trend-follow: trend is the primary driver, everything
            # else is confirmation only, never a blocker ─────────────
            # Short-duration binary trades reward riding the candle trend
            # that's already in motion, not waiting for a committee of
            # indicators to unanimously agree — that's what was causing
            # most cycles to skip. Trend decides direction whenever it
            # has any lean at all; the other indicators only add to the
            # confidence shown in the log when they happen to agree, they
            # never cancel or block a trend read.
            trend_vote = self._trend_signal(closes)

            if trend_vote == 0:
                logger.info(f"⏭️ No clear trend for {asset} — skipping cycle")
                return None, "No clear trend right now — skipping this cycle", True

            direction = "CALL" if trend_vote > 0 else "PUT"

            candidates: List[Tuple[str, float]] = [("Trend", trend_vote)]

            rsi = self._calculate_rsi(closes, period=14)
            if rsi is not None:
                rsi_vote = self._rsi_signal(rsi)
                if rsi_vote != 0:
                    candidates.append(("RSI", rsi_vote))

            if len(opens) >= 3:
                pattern_vote = self._candle_pattern_signal(
                    opens[-3:], closes[-3:], highs[-3:], lows[-3:]
                )
                if pattern_vote != 0:
                    candidates.append(("Pattern", pattern_vote))

                pattern3_vote = self._three_candle_pattern_signal(
                    opens[-3:], closes[-3:]
                )
                if pattern3_vote != 0:
                    candidates.append(("Pattern3", pattern3_vote))

            momentum_vote = self._momentum_signal(closes)
            if momentum_vote != 0:
                candidates.append(("Momentum", momentum_vote))

            sr_vote = self._support_resistance_signal(closes, highs, lows)
            if sr_vote != 0:
                candidates.append(("S/R", sr_vote))

            # Only the ones agreeing with the trend count as confirmation —
            # a contrary RSI/pattern reading doesn't block the trade, it
            # just doesn't strengthen it either.
            confirmations = [
                (name, vote) for name, vote in candidates
                if (vote > 0) == (trend_vote > 0)
            ]
            confidence = sum(abs(v) for _, v in confirmations)
            confirm_details = ", ".join(
                f"{name}({abs(vote):.1f})" for name, vote in confirmations
            )
            rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"

            logger.info(
                f"🔍 Signal: {direction} for {asset} "
                f"(trend={trend_vote:+.2f}, confidence={confidence:.2f}, RSI={rsi_str}) "
                f"[confirmed by: {confirm_details or 'trend only'}]"
            )
            detail = (
                f"Trend {direction} (RSI {rsi_str}) · "
                f"confirmed by {confirm_details or 'trend only'} · "
                f"confidence {confidence:.2f}"
            )
            return direction, detail, False

        except Exception as e:
            # Full detail goes to the server log for debugging — never the
            # raw exception text to the user-facing terminal feed, that
            # reads as a crash even when it's just a transient data gap.
            logger.error(
                f"Strategy analysis failed for {asset}: {e}. Skipping this cycle."
            )
            return None, "Live market data temporarily unavailable — skipping this cycle", True

    # ─────────────────────────────────────────────────────────────────────
    # Trend (fast/slow MA crossover + recent candle bias) — the primary
    # driver of direction; see _analyze for how everything else only
    # confirms rather than overrides this.
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _trend_signal(closes: List[float], fast: int = 5, slow: int = 20) -> float:
        """
        Continuous trend read: fast MA vs slow MA separation (the same
        basis as MACD) blended with recent candle direction bias. Meant
        to actually decide the trade, not just nudge a vote tally, so it
        stays non-zero whenever there's any real data — it only reads
        exactly flat on a genuinely dead/tied market.
        """
        if len(closes) < slow:
            return 0.0

        fast_sma = sum(closes[-fast:]) / fast
        slow_sma = sum(closes[-slow:]) / slow
        if slow_sma <= 0:
            return 0.0

        separation = (fast_sma - slow_sma) / slow_sma
        ma_component = max(-1.0, min(1.0, separation * 200))

        recent = closes[-6:]
        ups = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
        downs = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])
        candle_component = (ups - downs) / max(ups + downs, 1) * 0.3

        return max(-1.0, min(1.0, ma_component + candle_component))

    # ─────────────────────────────────────────────────────────────────────
    # RSI calculation
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _calculate_rsi(
        closes: List[float], period: int = 14
    ) -> Optional[float]:
        """Calculate RSI using exponential moving average method."""
        if len(closes) < period + 1:
            return None

        gains = []
        losses = []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))

        if len(gains) < period:
            return None

        # Initial SMA
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        # EMA smoothing for remaining periods
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _rsi_signal(rsi: float) -> float:
        """
        RSI-based mean reversion signal.

        Returns:
            Positive = CALL, Negative = PUT, 0 = neutral
        """
        if rsi <= 30:
            return 1.0   # Heavily oversold → strong CALL
        elif rsi <= 40:
            return 0.5   # Oversold zone → moderate CALL
        elif rsi >= 70:
            return -1.0  # Heavily overbought → strong PUT
        elif rsi >= 60:
            return -0.5  # Overbought zone → moderate PUT
        return 0.0        # Neutral zone

    # ─────────────────────────────────────────────────────────────────────
    # Candle pattern recognition — 2-candle
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _candle_pattern_signal(
        opens: List[float],
        closes: List[float],
        highs: List[float],
        lows: List[float],
    ) -> float:
        """
        Detect reversal candle patterns.

        Returns: Positive = CALL, Negative = PUT
        """
        if len(closes) < 2:
            return 0.0

        # Last candle properties
        body = closes[-1] - opens[-1]
        candle_range = highs[-1] - lows[-1]
        prev_body = closes[-2] - opens[-2]

        if candle_range == 0:
            return 0.0

        body_ratio = abs(body) / candle_range

        # ── Bullish engulfing → CALL ────────────────────────────────
        if prev_body < 0 and body > 0 and abs(body) > abs(prev_body):
            return 0.7

        # ── Bearish engulfing → PUT ─────────────────────────────────
        if prev_body > 0 and body < 0 and abs(body) > abs(prev_body):
            return -0.7

        # ── Hammer / Pin bar ────────────────────────────────────────
        lower_wick = min(opens[-1], closes[-1]) - lows[-1]
        upper_wick = highs[-1] - max(opens[-1], closes[-1])
        if candle_range > 0:
            lower_wick_ratio = lower_wick / candle_range
            upper_wick_ratio = upper_wick / candle_range
            if lower_wick_ratio > 0.6 and body_ratio < 0.3:
                return 0.5  # Hammer → CALL
            if upper_wick_ratio > 0.6 and body_ratio < 0.3:
                return -0.5  # Shooting star → PUT

        # ── Doji after run → reversal ───────────────────────────────
        if body_ratio < 0.1 and len(closes) >= 3:
            if closes[-2] < opens[-2] and closes[-3] < opens[-3]:
                return 0.3
            if closes[-2] > opens[-2] and closes[-3] > opens[-3]:
                return -0.3

        return 0.0

    # ─────────────────────────────────────────────────────────────────────
    # Candle pattern recognition — 3-candle
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _three_candle_pattern_signal(
        opens: List[float], closes: List[float]
    ) -> float:
        """
        Detect 3-candle continuation/reversal patterns: three white
        soldiers / three black crows (continuation), morning star /
        evening star (reversal).

        Returns: Positive = CALL, Negative = PUT
        """
        if len(opens) < 3 or len(closes) < 3:
            return 0.0

        o1, o2, o3 = opens[-3], opens[-2], opens[-1]
        c1, c2, c3 = closes[-3], closes[-2], closes[-1]

        # ── Three White Soldiers → CALL ─────────────────────────────
        if c1 > o1 and c2 > o2 and c3 > o3 and c2 > c1 and c3 > c2:
            return 0.5

        # ── Three Black Crows → PUT ─────────────────────────────────
        if c1 < o1 and c2 < o2 and c3 < o3 and c2 < c1 and c3 < c2:
            return -0.5

        # ── Morning Star (bearish, small body, bullish) → CALL ──────
        body1 = abs(c1 - o1)
        body2 = abs(c2 - o2)
        if c1 < o1 and body2 < body1 * 0.5 and c3 > o3 and c3 > (o1 + c1) / 2:
            return 0.5

        # ── Evening Star (bullish, small body, bearish) → PUT ───────
        if c1 > o1 and body2 < body1 * 0.5 and c3 < o3 and c3 < (o1 + c1) / 2:
            return -0.5

        return 0.0

    # ─────────────────────────────────────────────────────────────────────
    # Momentum divergence
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _momentum_signal(closes: List[float]) -> float:
        """
        Compare short-term vs medium-term momentum.
        Mean reversion / exhaustion detection.
        """
        if len(closes) < 10:
            return 0.0

        lookback = min(len(closes), 15)
        short_end = closes[-1]
        short_start = closes[-4] if len(closes) >= 4 else closes[0]
        med_start = closes[-lookback]

        short_roc = (short_end - short_start) / short_start if short_start > 0 else 0
        med_roc = (short_end - med_start) / med_start if med_start > 0 else 0

        # Overextended down → expect bounce CALL
        if short_roc < -0.001 and med_roc > short_roc:
            return 0.4

        # Overextended up → expect pullback PUT
        if short_roc > 0.001 and med_roc < short_roc:
            return -0.4

        return 0.0

    # ─────────────────────────────────────────────────────────────────────
    # Support / Resistance
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _support_resistance_signal(
        closes: List[float],
        highs: List[float],
        lows: List[float],
    ) -> float:
        """
        Simple support/resistance bounce detection.

        If price is near recent lows (support) → CALL
        If price is near recent highs (resistance) → PUT
        """
        if len(closes) < 15:
            return 0.0

        lookback = min(len(closes), 30)
        recent_highs = highs[-lookback:]
        recent_lows = lows[-lookback:]

        resistance = max(recent_highs)
        support = min(recent_lows)
        price_range = resistance - support

        if price_range <= 0:
            return 0.0

        current = closes[-1]
        position = (current - support) / price_range

        # Near support → CALL
        if position < 0.2:
            return 0.5
        elif position < 0.3:
            return 0.3

        # Near resistance → PUT
        if position > 0.8:
            return -0.5
        elif position > 0.7:
            return -0.3

        return 0.0
