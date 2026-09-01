"""
Risk Manager — Session P&L tracking, TP/SL enforcement, safety checks.

Tracks wins, losses, profit/loss, and enforces risk limits configured
via the Telegram settings menu.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from settings import TradingSettings

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """Record of a single trade execution."""
    asset: str
    direction: str
    amount: float
    result: str  # "win" | "loss" | "doji" | "error"
    profit: float  # +profit for win, -amount for loss, 0 for doji
    payout_pct: float
    martingale_step: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class RiskManager:
    """Tracks session P&L and enforces TP/SL/safety limits."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        """Reset all session counters (called on /start_trading)."""
        self.session_profit: float = 0.0
        self.session_trades: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.dojis: int = 0
        self.errors: int = 0
        self.consecutive_losses: int = 0
        self.consecutive_wins: int = 0
        self.peak_profit: float = 0.0
        self.max_drawdown: float = 0.0
        self.trade_history: List[TradeRecord] = []
        self.session_start: datetime = datetime.now(timezone.utc)
        logger.info("Risk manager reset — new session started.")

    # ─────────────────────────────────────────────────────────────────────
    # Record results
    # ─────────────────────────────────────────────────────────────────────

    def record_win(
        self,
        asset: str,
        direction: str,
        amount: float,
        payout_pct: float,
        martingale_step: int,
        total_invested: float,
    ) -> None:
        """Record a winning trade. Profit = (amount * payout_pct / 100) - losses from prior steps."""
        gross_payout = amount * payout_pct / 100.0
        # Net profit for this martingale sequence:
        # We won payout on the final step but invested total_invested across all steps
        net_profit = gross_payout - (total_invested - amount)
        self.session_profit += net_profit
        self.session_trades += 1
        self.wins += 1
        self.consecutive_losses = 0
        self.consecutive_wins += 1
        self.peak_profit = max(self.peak_profit, self.session_profit)

        self.trade_history.append(TradeRecord(
            asset=asset,
            direction=direction,
            amount=amount,
            result="win",
            profit=net_profit,
            payout_pct=payout_pct,
            martingale_step=martingale_step,
        ))
        logger.info(
            f"📊 WIN recorded: +${net_profit:.2f} "
            f"(session: ${self.session_profit:+.2f})"
        )

    def record_loss(
        self,
        asset: str,
        direction: str,
        total_invested: float,
        martingale_step: int,
    ) -> None:
        """Record a losing trade (all martingale steps failed)."""
        self.session_profit -= total_invested
        self.session_trades += 1
        self.losses += 1
        self.consecutive_losses += 1
        self.consecutive_wins = 0
        drawdown = self.peak_profit - self.session_profit
        self.max_drawdown = max(self.max_drawdown, drawdown)

        self.trade_history.append(TradeRecord(
            asset=asset,
            direction=direction,
            amount=total_invested,
            result="loss",
            profit=-total_invested,
            payout_pct=0,
            martingale_step=martingale_step,
        ))
        logger.info(
            f"📊 LOSS recorded: -${total_invested:.2f} "
            f"(session: ${self.session_profit:+.2f}, "
            f"consec losses: {self.consecutive_losses})"
        )

    def record_doji(
        self,
        asset: str,
        direction: str,
        amount: float,
        martingale_step: int,
    ) -> None:
        """Record a doji/tie (money returned)."""
        self.session_trades += 1
        self.dojis += 1
        # Consecutive counters stay unchanged

        self.trade_history.append(TradeRecord(
            asset=asset,
            direction=direction,
            amount=amount,
            result="doji",
            profit=0.0,
            payout_pct=0,
            martingale_step=martingale_step,
        ))
        logger.info("📊 DOJI recorded (money returned)")

    def record_error(self, asset: str, direction: str) -> None:
        """Record a trade execution error."""
        self.errors += 1
        self.trade_history.append(TradeRecord(
            asset=asset,
            direction=direction,
            amount=0,
            result="error",
            profit=0,
            payout_pct=0,
            martingale_step=0,
        ))

    # ─────────────────────────────────────────────────────────────────────
    # Safety checks
    # ─────────────────────────────────────────────────────────────────────

    def can_trade(
        self, settings: TradingSettings, balance: Optional[float] = None
    ) -> Tuple[bool, str]:
        """
        Check if trading should continue.

        Returns:
            (True, "OK") or (False, "reason to stop")
        """
        # Take Profit
        if self.session_profit >= settings.take_profit:
            return False, (
                f"🎯 Take Profit reached! "
                f"Session profit: ${self.session_profit:+.2f} "
                f"(target: ${settings.take_profit:.2f})"
            )

        # Stop Loss
        if self.session_profit <= -settings.stop_loss:
            return False, (
                f"🛑 Stop Loss hit! "
                f"Session loss: ${self.session_profit:+.2f} "
                f"(limit: -${settings.stop_loss:.2f})"
            )

        # Consecutive losses
        if self.consecutive_losses >= settings.max_consecutive_losses:
            return False, (
                f"❌ Max consecutive losses reached ({self.consecutive_losses}). "
                f"Emergency stop."
            )

        # Max trades per session
        if self.session_trades >= settings.max_trades:
            return False, (
                f"📋 Max trades reached ({self.session_trades}/{settings.max_trades}). "
                f"Session auto-stopped."
            )

        # Minimum balance
        if balance is not None and balance < settings.min_balance:
            return False, (
                f"💵 Balance ${balance:.2f} below minimum ${settings.min_balance:.2f}"
            )

        return True, "OK"

    # ─────────────────────────────────────────────────────────────────────
    # Statistics
    # ─────────────────────────────────────────────────────────────────────

    def get_win_rate(self) -> float:
        """Win rate as percentage (0-100)."""
        total = self.wins + self.losses
        if total == 0:
            return 0.0
        return (self.wins / total) * 100.0

    def get_session_duration(self) -> str:
        """Human-readable session duration."""
        elapsed = (datetime.now(timezone.utc) - self.session_start).total_seconds()
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def get_summary(self) -> Dict:
        """Get full statistics dict."""
        return {
            "session_profit": self.session_profit,
            "session_trades": self.session_trades,
            "wins": self.wins,
            "losses": self.losses,
            "dojis": self.dojis,
            "errors": self.errors,
            "win_rate": self.get_win_rate(),
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "peak_profit": self.peak_profit,
            "max_drawdown": self.max_drawdown,
            "session_duration": self.get_session_duration(),
        }

    def summary_text(self) -> str:
        """Formatted summary for Telegram."""
        wr = self.get_win_rate()
        streak = (
            f"🔥 +{self.consecutive_wins}"
            if self.consecutive_wins > 0
            else f"❄️ -{self.consecutive_losses}"
            if self.consecutive_losses > 0
            else "➖ 0"
        )
        profit_emoji = "🟢" if self.session_profit >= 0 else "🔴"

        lines = [
            f"{profit_emoji} <b>Session P&L:</b> ${self.session_profit:+.2f}",
            f"",
            f"✅ Wins: {self.wins}",
            f"❌ Losses: {self.losses}",
            f"⚖️ Dojis: {self.dojis}",
            f"📈 Win Rate: {wr:.1f}%",
            f"{streak}",
            f"",
            f"📊 Total Trades: {self.session_trades}",
            f"📉 Max Drawdown: ${self.max_drawdown:.2f}",
            f"⏱️ Duration: {self.get_session_duration()}",
        ]
        return "\n".join(lines)
