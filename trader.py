"""
PocketOption Trade Executor.

Handles connection, trade placement, result polling, and martingale sequences.
Reuses proven patterns from the main_bot reference implementation.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable, Optional, Tuple

from candle_timing import seconds_until_next_candle

# Called after each martingale step is placed, and again once its result is
# known — lets the caller (session_manager) surface a live per-step feed
# instead of only ever seeing the final win/loss of the whole sequence.
StepCallback = Optional[Callable[[dict], Awaitable[None]]]

from config import ASSET_LOAD_WAIT_SECONDS
from settings import TradingSettings

logger = logging.getLogger(__name__)


class TradeResult(Enum):
    """Trade result enumeration."""
    WIN = "win"
    LOSS = "loss"
    DOJI = "doji"
    ERROR = "error"
    INVALID_ASSET = "invalid_asset"
    PENDING = "pending"


class PocketOptionTrader:
    """
    Executes trades directly with PocketOption using SSID.
    Uses BinaryOptionsToolsV2 library.
    """

    def __init__(self):
        self.api = None
        self.is_connected = False
        self.current_ssid_index = 0
        self._balance_cache: Optional[float] = None
        self._balance_cache_at: float = 0.0
        self._balance_inflight: Optional[asyncio.Task] = None

    # ─────────────────────────────────────────────────────────────────────
    # Connection
    # ─────────────────────────────────────────────────────────────────────

    # Real-money regions we've confirmed reachable — see connect()'s own
    # comment for why only these two, never the Russia-only ones.
    _REAL_URLS_EU_FIRST = [
        "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket",
        "wss://api-us-south.po.market/socket.io/?EIO=4&transport=websocket",
    ]
    _REAL_URLS_US_FIRST = [
        "wss://api-us-south.po.market/socket.io/?EIO=4&transport=websocket",
        "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket",
    ]

    async def connect(self, ssid: str, region_hint: int = 0) -> bool:
        """
        Connect to PocketOption API using the provided SSID.

        region_hint: which of the two real-money regions to try FIRST —
        alternates (0=EU, 1=US-South, 2=EU, ...) across session_manager.py's
        internal reconnect attempts. This matters more than it looks: the
        library only tries the second URL in `urls` if the first one fails
        to connect outright. Our actual failure mode is different — the
        connection succeeds (handshake completes) but the balance/data
        stream just never arrives (see get_balance()'s docstring) — which
        the library doesn't treat as a connect failure at all, so its own
        fallback to the second URL never triggers. Every retry was
        therefore silently hitting the exact same server, every time, no
        matter how many times session_manager.py reconnected. Alternating
        which URL we hand it as the *first* choice is what actually gives
        a stuck session a real shot at a different server.
        """
        if not ssid:
            logger.error("No valid SSID provided!")
            return False

        try:
            from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync
            from BinaryOptionsToolsV2.config import Config
        except ImportError:
            logger.error(
                "BinaryOptionsToolsV2 not installed! "
                "Run: pip install binaryoptionstoolsv2"
            )
            return False

        # If the account's own nearest server (picked via its SSID's embedded
        # geo data) fails, the library's own fallback walks PocketOption's
        # full region list — which includes RUSSIA_MOSCOW / RUSSIA_SPB. For
        # a non-Russian REAL account those get hard-rejected by PocketOption's
        # own server right after the handshake ("Connection closed before
        # authentication was completed"), wasting the whole retry budget on
        # a region that was never going to work. Restrict fallback to the
        # two western real-money regions we've confirmed are reachable — but
        # only for real accounts. Demo has exactly one server platform-wide
        # (demo-api-eu.po.market); pointing a demo SSID at these real-money
        # URLs on fallback would just fail auth outright, so leave demo's
        # own single-server retry behavior untouched.
        is_demo_match = re.search(r'"isDemo"\s*:\s*(\d)', ssid)
        is_demo = bool(is_demo_match) and is_demo_match.group(1) == "1"
        cfg_kwargs = {"connection_initialization_timeout_secs": 90}
        if not is_demo:
            cfg_kwargs["urls"] = (
                self._REAL_URLS_US_FIRST if region_hint % 2 else self._REAL_URLS_EU_FIRST
            )
        cfg = Config(**cfg_kwargs)

        try:
            region_note = ""
            if not is_demo:
                region_note = f" (preferring {'US-South' if region_hint % 2 else 'EU'})"
            logger.info(f"Connecting to PocketOption...{region_note}")
            # PocketOptionAsync(...) is a plain synchronous constructor —
            # it blocks the calling coroutine for however long the library's
            # own connection_initialization_timeout_secs takes (up to 90s
            # here), with no `await` inside it for asyncio to hook a
            # cancellation into. Pressing Stop while stuck in this call did
            # nothing until it finished on its own — task.cancel() has no
            # checkpoint to interrupt at. Running it in a thread via
            # to_thread() gives the `await` below a real cancellation point:
            # Stop now returns control immediately even though the
            # background thread keeps running until the library's own
            # timeout fires (harmless — its result is just discarded).
            self.api = await asyncio.to_thread(PocketOptionAsync, ssid, config=cfg)
            logger.info(
                f"   Waiting {ASSET_LOAD_WAIT_SECONDS}s for assets to load..."
            )
            await asyncio.sleep(ASSET_LOAD_WAIT_SECONDS)
        except asyncio.CancelledError:
            self.api = None
            raise
        except Exception as e:
            logger.error(f"PocketOption connection failed: {e}")
            self.api = None
            return False

        # The websocket session itself is live once the object above is
        # constructed and assets have loaded — buy/sell/check_win all work
        # off that, independent of this balance probe. Treating a slow/
        # timed-out balance() as a failed connection was forcing a full
        # reconnect (jitter + asset-load wait + this timeout, ~30-40s) on
        # every single trading cycle, since is_connected never got set.
        self.is_connected = True
        # asyncio.wait() rather than wait_for(): see get_balance()'s _fetch()
        # for why — wait_for hangs past its own timeout if the underlying
        # Rust call ignores the cancellation it sends on expiry.
        probe_task = asyncio.ensure_future(self.api.balance())
        done, pending = await asyncio.wait({probe_task}, timeout=15.0)
        if probe_task in pending:
            probe_task.cancel()
            logger.warning("Connected, but initial balance check timed out")
        else:
            try:
                logger.info(f"✅ Connected to PocketOption! Balance: ${probe_task.result():.2f}")
            except Exception as e:
                logger.warning(f"Connected, but initial balance check failed: {e}")
        return True

    async def get_balance(self) -> Optional[float]:
        """
        Get current account balance.

        The frontend polls /api/status every 3s, which calls this on every
        poll. Under degraded network conditions a single balance() call can
        take longer than that, so naively firing one per poll piles up
        overlapping in-flight requests to PocketOption — exactly the kind
        of burst that risks tripping their own rate-limiting (see the
        connection blackhole investigation this session). Instead: reuse
        one in-flight call across concurrent pollers, and serve a short-
        lived cached value instead of starting a new call at all when one
        completed recently.
        """
        if not self.api:
            return None

        now = asyncio.get_event_loop().time()
        if self._balance_cache is not None and (now - self._balance_cache_at) < 2.5:
            return self._balance_cache

        if self._balance_inflight is not None and not self._balance_inflight.done():
            try:
                return await self._balance_inflight
            except Exception:
                return None

        async def _fetch() -> Optional[float]:
            # NOT asyncio.wait_for(..., timeout=20.0): on timeout it cancels
            # the inner call and then *awaits that cancellation completing*
            # before raising — if the Rust/PyO3 bridge underneath balance()
            # doesn't actually respond to that cancellation (a known async
            # FFI gotcha), wait_for hangs right past its own "hard" timeout,
            # which is exactly the 20+s stall this was supposed to prevent
            # and was still showing up in /api/status. asyncio.wait() with a
            # timeout returns control the moment the deadline passes
            # regardless of whether the underlying task ever stops — we
            # still call cancel() for cleanup, but never wait on it.
            balance_task = asyncio.ensure_future(self.api.balance())
            done, pending = await asyncio.wait({balance_task}, timeout=8.0)
            if balance_task in pending:
                balance_task.cancel()
                logger.error("Failed to get balance: timed out (underlying call did not respond to cancellation)")
                return None
            try:
                bal = balance_task.result()
                self._balance_cache = bal
                self._balance_cache_at = asyncio.get_event_loop().time()
                return bal
            except Exception as e:
                logger.error(f"Failed to get balance: {e}")
                return None

        self._balance_inflight = asyncio.ensure_future(_fetch())
        try:
            return await self._balance_inflight
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return None

    async def get_payout(self, asset: str) -> Optional[float]:
        """Get payout percentage for an asset."""
        if not self.api:
            return None
        # asyncio.wait() rather than wait_for(): see get_balance()'s
        # _fetch() for why.
        payout_task = asyncio.ensure_future(self.api.payout(asset))
        done, pending = await asyncio.wait({payout_task}, timeout=10.0)
        if payout_task in pending:
            payout_task.cancel()
            logger.debug(f"Payout lookup timed out for {asset}")
            return None
        try:
            payout = payout_task.result()
            if isinstance(payout, (int, float)):
                return float(payout)
        except Exception as e:
            logger.debug(f"Payout lookup failed for {asset}: {e}")
        return None

    # ─────────────────────────────────────────────────────────────────────
    # Asset normalization
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def normalize_asset(asset: str) -> str:
        """
        Normalize asset name for PocketOption trading.

        Signal display: EURUSD-OTC
        Trading uses:   EURUSD_otc
        """
        asset = asset.strip()

        # Convert -OTC to _otc
        if "-OTC" in asset.upper():
            asset = asset.upper().replace("-OTC", "_otc")

        # Ensure _otc is lowercase
        if "_OTC" in asset:
            asset = asset.replace("_OTC", "_otc")

        # If no _otc suffix, add it (OTC pairs are available 24/7)
        if "_otc" not in asset.lower():
            asset = asset.upper() + "_otc"
            logger.info(f"📌 Added _otc suffix: {asset}")

        return asset

    # ─────────────────────────────────────────────────────────────────────
    # Trade execution
    # ─────────────────────────────────────────────────────────────────────

    async def execute_martingale(
        self,
        asset: str,
        direction: str,
        settings: TradingSettings,
        on_step: StepCallback = None,
        committed_session_profit: float = 0.0,
        starting_balance: Optional[float] = None,
    ) -> Tuple[str, int, float, float, Optional[str], Optional[str]]:
        """
        Execute a full martingale trade sequence.

        Args:
            asset: Trading pair
            direction: "CALL" or "PUT"
            settings: Current trading settings (amounts, steps from Telegram menu)
            on_step: optional async callback invoked once a step is placed
                and again once that step's result is known — each call
                receives a dict describing what just happened.
            committed_session_profit: risk_manager.session_profit as of the
                start of this cycle — the real, already-realized P&L from
                every *completed* cycle. Used to gate each step below
                against Stop Loss before money moves (see the loop body).
            starting_balance: account balance as of the start of this cycle.
                Used to gate each step against Min Balance the same way —
                otherwise a martingale sequence's escalating stakes can
                drive the real balance below the configured floor before
                the next per-cycle balance check ever runs.

        Returns:
            (result, final_step, total_invested, payout_pct, last_trade_id, risk_reason)
            result: "win" | "loss" | "doji" | "error" | "invalid_asset" | "risk_stop"
            "risk_stop": placing this step (if it lost) would have breached
                Stop Loss or Min Balance, so it was never placed —
                total_invested only covers steps that actually executed;
                risk_reason explains which limit stopped it (None otherwise).
        """
        if not self.is_connected or not self.api:
            logger.error("Not connected to PocketOption")
            return "error", 0, 0.0, 0.0, None, None

        asset = self.normalize_asset(asset)
        max_steps = settings.get_max_steps()
        total_invested = 0.0
        last_payout_pct = 0.0
        last_trade_id: Optional[str] = None

        for step in range(1, max_steps + 1):
            amount = settings.get_martingale_amount(step)

            # Pre-trade risk gate: Stop Loss and Min Balance are normally
            # only checked between cycles, which lets a martingale sequence
            # blow straight through either limit before the next check ever
            # runs (a 3-step sequence can lose several times the SL in one
            # go, or drain the account well past the configured floor).
            # Check the *worst case* of this specific step — if it also
            # loses — against both limits before the money is risked, and
            # stop instead of placing it if either would be breached.
            worst_case_profit = committed_session_profit - (total_invested + amount)
            risk_reason = None
            if worst_case_profit <= -settings.stop_loss:
                risk_reason = (
                    f"a loss on step {step} would put the session at ${worst_case_profit:+.2f}, "
                    f"past Stop Loss (-${settings.stop_loss:.2f})"
                )
            elif starting_balance is not None:
                worst_case_balance = starting_balance - (total_invested + amount)
                if worst_case_balance < settings.min_balance:
                    risk_reason = (
                        f"a loss on step {step} would drop the balance to ${worst_case_balance:.2f}, "
                        f"below Min Balance (${settings.min_balance:.2f})"
                    )
            if risk_reason:
                logger.warning(f"🛑 [RISK] Step {step}/{max_steps} (${amount:.2f}) skipped — {risk_reason}. Stopping instead.")
                return "risk_stop", step - 1, total_invested, last_payout_pct, last_trade_id, risk_reason

            total_invested += amount

            logger.info(
                f"🎯 [TRADE] Step {step}/{max_steps}: "
                f"{asset} {direction} ${amount:.2f}"
            )

            result, payout_pct, trade_id = await self._place_trade(
                asset, direction, amount, settings.trade_duration
            )
            last_payout_pct = payout_pct or 0.0
            last_trade_id = trade_id or last_trade_id

            if on_step:
                await on_step({
                    "step": step, "max_steps": max_steps, "amount": amount,
                    "result": result.value, "trade_id": trade_id,
                })

            if result == TradeResult.WIN:
                logger.info(
                    f"✅ [WIN] Step {step} — "
                    f"Total invested: ${total_invested:.2f}"
                )
                return "win", step, total_invested, last_payout_pct, last_trade_id, None

            elif result == TradeResult.DOJI:
                logger.info(f"⚖️ [DOJI] Step {step} — money returned")
                return "doji", step, total_invested, last_payout_pct, last_trade_id, None

            elif result == TradeResult.INVALID_ASSET:
                logger.error(f"🚫 [INVALID ASSET] Step {step}")
                return "invalid_asset", step, total_invested, 0.0, last_trade_id, None

            elif result == TradeResult.ERROR:
                logger.error(f"⚠️ [ERROR] Step {step}")
                return "error", step, total_invested, 0.0, last_trade_id, None

            elif result == TradeResult.LOSS:
                if step < max_steps:
                    next_amount = settings.get_martingale_amount(step + 1)
                    logger.info(
                        f"❌ [LOSS] Step {step}. "
                        f"Next step: ${next_amount:.2f}"
                    )
                    # _place_trade already blocked until this trade expired,
                    # so we're already close to the next candle boundary —
                    # only wait the small remainder, not another full
                    # duration on top of it.
                    align_wait = seconds_until_next_candle(settings.trade_duration)
                    if align_wait > 0:
                        await asyncio.sleep(align_wait)
                else:
                    logger.warning(
                        f"❌ [LOSS] Final step {step}. "
                        f"Martingale sequence failed."
                    )

        return "loss", max_steps, total_invested, last_payout_pct, last_trade_id, None

    async def _place_trade(
        self,
        asset: str,
        direction: str,
        amount: float,
        duration: int,
    ) -> Tuple[TradeResult, Optional[float], Optional[str]]:
        """
        Place a single trade and wait for result.

        Returns:
            (TradeResult, payout_percentage_or_None, trade_id_or_None)
        """
        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                log_suffix = (
                    f" (retry {attempt + 1}/{max_retries})"
                    if attempt > 0
                    else ""
                )
                logger.info(f"📊 Placing trade: {asset}{log_suffix}")

                if direction.upper() == "CALL":
                    trade_id, trade_data = await self.api.buy(
                        asset=asset,
                        amount=amount,
                        time=duration,
                        check_win=False,
                    )
                else:
                    trade_id, trade_data = await self.api.sell(
                        asset=asset,
                        amount=amount,
                        time=duration,
                        check_win=False,
                    )

                # Extract payout percentage from order ticket
                payout_pct: Optional[float] = None
                if isinstance(trade_data, dict):
                    raw = trade_data.get("percentProfit")
                    if isinstance(raw, (int, float)):
                        payout_pct = float(raw)

                logger.info(f"📤 Trade placed: {trade_id}")

                # Poll for result with generous timeout
                # By polling immediately without sleeping, we ensure we don't miss the result.
                # CRITICAL: check_win can take >60s in live conditions.
                # Short timeouts cause false LOSS and unwanted martingale.
                result = await self._poll_result(trade_id)

                if result is not None:
                    return result, payout_pct, trade_id
                else:
                    logger.error(
                        "Result polling failed — "
                        "stopping as ERROR (not forcing LOSS)."
                    )
                    return TradeResult.ERROR, payout_pct, trade_id

            except Exception as e:
                error_str = str(e).lower()

                # Invalid asset — no retry
                if any(
                    msg in error_str
                    for msg in ("invalid asset", "not active", "asset is not")
                ):
                    logger.error(f"❌ Invalid asset: {e}")
                    return TradeResult.INVALID_ASSET, None, None

                # Assets not loaded — retry with delay
                if (
                    "assets not loaded" in error_str
                    and attempt < max_retries - 1
                ):
                    logger.warning(
                        f"Assets not loaded, waiting {retry_delay}s "
                        f"and retrying ({attempt + 1}/{max_retries})..."
                    )
                    await asyncio.sleep(retry_delay)
                    continue

                logger.error(f"Trade execution error: {e}")
                return TradeResult.ERROR, None, None

        return TradeResult.ERROR, None, None

    async def _poll_result(self, trade_id) -> Optional[TradeResult]:
        """
        Poll check_win until a terminal result is received.

        Uses generous timeouts to avoid false LOSS results.
        """
        per_call_timeout = 65.0
        max_polls = 3
        poll_delay = 0.7

        logger.info(f"⏳ Waiting for trade result (up to {int(per_call_timeout * max_polls)}s)...")

        for poll in range(max_polls):
            # asyncio.wait() rather than wait_for(): see get_balance()'s
            # _fetch() for why — this is what a martingale sequence's
            # entire trading loop was waiting behind, so an unresponsive
            # cancellation here could stall real trading far longer than
            # per_call_timeout was ever meant to allow.
            check_task = asyncio.ensure_future(self.api.check_win(trade_id))
            done, pending = await asyncio.wait({check_task}, timeout=per_call_timeout)
            if check_task in pending:
                check_task.cancel()
                logger.info(
                    f"⚠️ Poll {poll + 1}/{max_polls}: "
                    f"timeout after {per_call_timeout:.0f}s"
                )
                await asyncio.sleep(poll_delay)
                continue
            try:
                result_data = check_task.result()
                logger.info(f"🔍 Poll {poll + 1}: {result_data}")

                parsed = self._parse_result(result_data)
                if parsed in (TradeResult.WIN, TradeResult.LOSS, TradeResult.DOJI):
                    return parsed
            except Exception as e:
                logger.warning(f"⚠️ Poll {poll + 1}/{max_polls} error: {e}")

            await asyncio.sleep(poll_delay)

        return None

    @staticmethod
    def _parse_result(result_data) -> TradeResult:
        """Parse BinaryOptionsToolsV2 check_win payload."""
        result_str = ""
        profit = None

        if isinstance(result_data, dict):
            raw = result_data.get("result")
            result_str = str(raw).strip().lower() if raw is not None else ""
            profit = result_data.get("profit")
        elif isinstance(result_data, str):
            result_str = result_data.strip().lower()
        elif result_data is not None:
            result_str = str(result_data).strip().lower()

        if result_str in ("win", "won", "true", "1"):
            return TradeResult.WIN
        if result_str in ("doji", "tie", "draw", "equal", "refund", "return"):
            return TradeResult.DOJI
        if result_str in ("loss", "lose", "lost", "false", "0", "-1"):
            return TradeResult.LOSS

        # Fallback to profit sign
        if isinstance(profit, (int, float)):
            if profit > 0:
                return TradeResult.WIN
            if profit < 0:
                return TradeResult.LOSS

        return TradeResult.PENDING
