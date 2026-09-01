"""
Settings Manager — JSON-persisted trading settings.

All trading parameters are stored here with sensible defaults.
Users change them via Telegram menus; changes are saved to settings.json
and survive bot restarts.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default path for the settings file (next to this script)
_DEFAULT_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")


@dataclass
class TradingSettings:
    """All user-configurable trading parameters."""

    # ── Account ──────────────────────────────────────────────────────────
    account_mode: str = "DEMO"  # "DEMO" | "REAL"
    demo_ssid: str = ""
    real_ssid: str = ""

    # Has this user clicked through the real-money risk disclosure at least
    # once? Required before a real_ssid can be saved or REAL-mode trading
    # started — see /api/risk-ack in server.py. Keeps a timestamp so there's
    # an actual record of when a customer accepted the risk, not just a
    # bare flag.
    real_risk_ack: bool = False
    real_risk_ack_at: Optional[str] = None

    # ── Trading ──────────────────────────────────────────────────────────
    asset: str = "EURUSD_otc"
    direction: str = "AUTO"  # "AUTO" | "CALL" | "PUT"
    base_amount: float = 1.0
    trade_duration: int = 5  # expiry in seconds
    trade_interval: int = 60  # seconds between trades

    # ── Martingale ───────────────────────────────────────────────────────
    martingale_enabled: bool = True
    martingale_max_steps: int = 3
    martingale_multiplier: float = 2.5
    # If set, overrides multiplier-based calculation. e.g. [1.0, 2.5, 6.0]
    martingale_custom_amounts: Optional[List[float]] = None

    # ── Risk Management ──────────────────────────────────────────────────
    take_profit: float = 50.0  # $ session profit → auto-pause
    stop_loss: float = 25.0  # $ session loss → auto-pause
    max_consecutive_losses: int = 10  # emergency stop
    max_trades: int = 20  # auto-stop after N trades per session
    min_balance: float = 5.0  # don't trade below this balance

    # ── Display ──────────────────────────────────────────────────────────
    timezone_offset: int = -5  # UTC offset for display times

    # ── All-time stats (persisted across sessions) ────────────────────────
    overall_wins: int = 0
    overall_losses: int = 0

    # ─────────────────────────────────────────────────────────────────────
    # Persistence
    # ─────────────────────────────────────────────────────────────────────

    def save(self, path: str = _DEFAULT_SETTINGS_PATH) -> None:
        """Save settings to JSON file."""
        data = asdict(self)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info(f"Settings saved to {path}")
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")

    @classmethod
    def load(cls, path: str = _DEFAULT_SETTINGS_PATH) -> "TradingSettings":
        """Load settings from JSON, falling back to defaults for missing keys."""
        settings = cls()
        if not os.path.exists(path):
            logger.info(f"No settings file found at {path}; using defaults.")
            settings.save(path)
            return settings
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Only apply known fields (ignore stale/unknown keys)
            defaults = asdict(settings)
            for key, default_val in defaults.items():
                if key in data:
                    setattr(settings, key, data[key])
            logger.info(f"Settings loaded from {path}")
        except Exception as e:
            logger.error(f"Failed to load settings from {path}: {e}; using defaults.")
        return settings

    def to_dict(self) -> dict:
        """Plain-dict form for database storage (see user_store.py)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TradingSettings":
        """Inverse of to_dict() — ignores unknown/stale keys, same as load()."""
        settings = cls()
        defaults = asdict(settings)
        for key in defaults:
            if key in data:
                setattr(settings, key, data[key])
        return settings

    # ─────────────────────────────────────────────────────────────────────
    # Validation
    # ─────────────────────────────────────────────────────────────────────

    def validate(self) -> Tuple[bool, List[str]]:
        """Validate all settings. Returns (is_valid, list_of_errors)."""
        errors: List[str] = []

        if self.base_amount <= 0:
            errors.append("Trade amount must be > 0")
        if self.base_amount > 10000:
            errors.append("Trade amount seems too high (> $10,000)")

        if self.trade_duration < 5:
            errors.append("Expiry time must be >= 5 seconds")
        if self.trade_duration > 86400:
            errors.append("Expiry time must be <= 86400 seconds (1 day)")

        if self.trade_interval < 5:
            errors.append("Trade interval must be >= 5 seconds")



        if self.account_mode not in ("DEMO", "REAL"):
            errors.append("Account mode must be DEMO or REAL")

        if not self.asset or not isinstance(self.asset, str):
            errors.append("Asset must be a non-empty string")

        if self.martingale_max_steps < 1:
            errors.append("Martingale max steps must be >= 1")
        if self.martingale_max_steps > 10:
            errors.append("Martingale max steps should be <= 10 for safety")

        if self.martingale_multiplier < 1.0:
            errors.append("Martingale multiplier must be >= 1.0")
        if self.martingale_multiplier > 10.0:
            errors.append("Martingale multiplier should be <= 10.0 for safety")

        if self.martingale_custom_amounts is not None:
            if not isinstance(self.martingale_custom_amounts, list):
                errors.append("Custom amounts must be a list of numbers")
            elif len(self.martingale_custom_amounts) == 0:
                errors.append("Custom amounts list cannot be empty")
            elif any(a <= 0 for a in self.martingale_custom_amounts):
                errors.append("All custom amounts must be > 0")

        if self.take_profit <= 0:
            errors.append("Take profit must be > 0")
        if self.stop_loss <= 0:
            errors.append("Stop loss must be > 0")
        if self.max_consecutive_losses < 1:
            errors.append("Max consecutive losses must be >= 1")
        if self.max_trades < 1:
            errors.append("Max trades must be >= 1")
        if self.min_balance < 0:
            errors.append("Min balance must be >= 0")

        return (len(errors) == 0, errors)

    # ─────────────────────────────────────────────────────────────────────
    # Martingale helpers
    # ─────────────────────────────────────────────────────────────────────

    def get_martingale_amount(self, step: int) -> float:
        """
        Get the trade amount for a given martingale step (1-indexed).

        Step 1 = base amount.
        Step 2+ = multiplied or from custom list.
        """
        if step < 1:
            return self.base_amount

        if self.martingale_custom_amounts:
            if step <= len(self.martingale_custom_amounts):
                return self.martingale_custom_amounts[step - 1]
            return self.martingale_custom_amounts[-1]

        # Multiplier-based: base * multiplier^(step-1)
        return round(self.base_amount * (self.martingale_multiplier ** (step - 1)), 2)

    def get_max_steps(self) -> int:
        """Get effective max martingale steps."""
        if not self.martingale_enabled:
            return 1
        return self.martingale_max_steps

    # ─────────────────────────────────────────────────────────────────────
    # Display helpers
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def format_duration(seconds: int) -> str:
        """Format duration to human-readable string."""
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            m = seconds // 60
            s = seconds % 60
            return f"{m}m{s}s" if s else f"{m}m"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h{m}m" if m else f"{h}h"

    def get_display_asset(self) -> str:
        """Return channel-facing asset name."""
        asset = self.asset
        if asset == "UKBrent_otc":
            return "Brent Oil OTC"
        base = asset.replace("_otc", "").upper()
        return f"{base}-OTC"

    def summary_text(self) -> str:
        """Multi-line summary of current settings for Telegram admin display."""
        mg_status = "ON" if self.martingale_enabled else "OFF"
        if self.martingale_custom_amounts:
            mg_amounts = " → ".join(
                f"${a:.2f}" for a in self.martingale_custom_amounts
            )
            mg_detail = f"Custom: {mg_amounts}"
        else:
            mg_detail = f"{self.martingale_multiplier}x multiplier"

        lines = [
            f"👤 <b>Account:</b> {self.account_mode}",
            f"📊 <b>Asset:</b> {self.get_display_asset()}",
            f"💰 <b>Amount:</b> ${self.base_amount:.2f}",
            f"⏱️ <b>Expiry:</b> {self.format_duration(self.trade_duration)}",
            f"🔄 <b>Interval:</b> {self.format_duration(self.trade_interval)}",
            f"",
            f"📈 <b>Martingale:</b> {mg_status}",
            f"   Steps: {self.martingale_max_steps} | {mg_detail}",
            f"",
            f"🎯 <b>Take Profit:</b> ${self.take_profit:.2f}",
            f"🛑 <b>Stop Loss:</b> ${self.stop_loss:.2f}",
            f"❌ <b>Max Consec. Losses:</b> {self.max_consecutive_losses}",
            f"📋 <b>Max Trades:</b> {self.max_trades}",
            f"💵 <b>Min Balance:</b> ${self.min_balance:.2f}",
        ]
        return "\n".join(lines)

    def channel_summary_text(self) -> str:
        """Clean summary for channel subscribers (no internal settings like direction mode)."""
        mg_status = "ON" if self.martingale_enabled else "OFF"
        lines = [
            f"📊 <b>Asset:</b> {self.get_display_asset()}",
            f"💰 <b>Amount:</b> ${self.base_amount:.2f}",
            f"⏱️ <b>Expiry:</b> {self.format_duration(self.trade_duration)}",
            f"📈 <b>Martingale:</b> {mg_status} ({self.martingale_max_steps} steps)",
            f"🎯 <b>TP:</b> ${self.take_profit:.2f} | 🛑 <b>SL:</b> ${self.stop_loss:.2f}",
        ]
        return "\n".join(lines)
