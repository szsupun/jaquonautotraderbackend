"""
Multi-user trading sessions — one isolated PocketOption connection + trading
loop per Telegram user, all running concurrently inside a single backend
process, none able to stall another.

Why this doesn't get "stuck":
  - Each user gets their own PocketOptionTrader + RiskManager + asyncio.Task.
    An exception or hang in one user's task never touches anyone else's —
    asyncio tasks are isolated by design; we also wrap every loop body in
    try/except so a single bad response can't kill the task itself.
  - trader.py already applies hard timeouts to every PocketOption call
    (connect, balance, buy/sell, check_win), so a single stuck user can only
    ever block *their own* task for that timeout, never the process.
  - CONNECT_SEMAPHORE caps how many PocketOption logins happen at once
    (login/asset-load is the slowest step, ~20s) so a burst of users hitting
    Start together can't hammer PocketOption and get everyone rate-limited.
  - CONNECT_JITTER_MAX_SECONDS spreads that burst out further.
  - MAX_CONCURRENT_TRADERS caps total simultaneous trading sessions so the
    host machine / outbound IP has a known ceiling.
  - Per-user exponential backoff on repeated errors (same pattern as the
    original single-account loop), scoped to that user only.
"""

from __future__ import annotations

import asyncio
import logging
import random
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Optional, Tuple

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

from config import (
    ASSET_LOAD_WAIT_SECONDS,
    CONNECT_JITTER_MAX_SECONDS,
    MAX_CONCURRENT_CONNECTS,
    MAX_CONCURRENT_TRADERS,
)
from candle_timing import seconds_until_next_candle
from connect_limit_store import check_and_record_session_start
from error_messages import humanize_error
from relay_control import activate_demo_relay, select_demo_relay
from risk_manager import RiskManager
from session_history_store import append_session
from settings import TradingSettings
from strategy import TradingStrategy
from trader import PocketOptionTrader
from user_store import load_user_settings, save_user_settings

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 10
# How many times a fresh Start auto-retries a failed connect before giving
# up and stopping the session. Previously a single failed connect (e.g. a
# PocketOption-side hiccup) stopped the whole session immediately, forcing
# the customer to notice and press Start again by hand — this gives a
# transient blip a few automatic chances to clear on its own first.
MAX_CONNECT_RETRIES = 3


@dataclass
class UserSession:
    user_id: int
    settings: TradingSettings
    trader: PocketOptionTrader = field(default_factory=PocketOptionTrader)
    risk_manager: RiskManager = field(default_factory=RiskManager)
    trading_active: bool = False
    connecting: bool = False  # True only while a connect() call is in flight
    connect_eta: Optional[datetime] = None  # estimated finish time of the current connect
    # Which automatic retry this is (1-indexed) and the cap, so the Mini
    # App can show "Attempt 2 of 3" instead of looking stuck on a retry.
    connect_attempt: int = 0
    connect_max_attempts: int = 0
    next_trade_at: Optional[datetime] = None  # for the frontend's live countdown
    task: Optional[asyncio.Task] = None
    last_error: Optional[str] = None
    # Raw exception text (type + message) behind a customer-friendly
    # last_error — admin-panel only, never shown to the customer directly.
    last_error_detail: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Live event feed for the Mini App's Terminal tab — bounded so a long
    # session can't grow this unbounded.
    logs: Deque[dict] = field(default_factory=lambda: deque(maxlen=300))
    # Running, not-yet-committed P&L for the *current* in-flight martingale
    # cycle — goes more negative as each losing step spends money, reset to
    # 0 the instant the cycle actually resolves (risk_manager.session_profit
    # is the real, final number by then). Display-only: TP/SL enforcement
    # in can_trade() never reads this, only the committed session_profit —
    # a step 1 loss might still get recovered by step 2, so it would be
    # wrong to treat it as final for actually stopping trading. This exists
    # purely so the UI (equity chart, TP/SL bars) can show real money moving
    # after every single trade placement, not just once per full cycle.
    live_step_pnl: float = 0.0


class SessionManager:
    """Owns every user's isolated trading session."""

    def __init__(self, bot=None) -> None:
        self._sessions: Dict[int, UserSession] = {}
        self._strategy = TradingStrategy()  # stateless — safe to share
        self._connect_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONNECTS)
        self._bot = bot  # aiogram Bot — used to DM each user their own signals

    def _tlog(self, session: "UserSession", message: str, level: str = "info") -> None:
        """Append to this user's live terminal feed (Mini App Terminal tab)."""
        session.logs.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
        })

    async def _dm(self, user_id: int, text: str) -> None:
        """Best-effort DM to the trading user themself — no shared channel."""
        if not self._bot:
            return
        try:
            await self._bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
        except TelegramAPIError as e:
            logger.warning(f"[user {user_id}] DM failed: {e}")

    async def send_document(self, user_id: int, filename: str, content: bytes, caption: str = "") -> bool:
        """Send a file straight into the user's own chat with the bot —
        used for CSV export. Deliberately not a downloadable link: a link
        opened in the system browser exposes this server's real address to
        the customer (see the sslip.io discussion), while a bot-delivered
        document never leaves Telegram's own infrastructure and needs no
        public URL at all. Returns whether it actually sent."""
        if not self._bot:
            return False
        try:
            await self._bot.send_document(
                chat_id=user_id,
                document=BufferedInputFile(content, filename=filename),
                caption=caption,
            )
            return True
        except TelegramAPIError as e:
            logger.warning(f"[user {user_id}] send_document failed: {e}")
            return False

    async def broadcast(self, user_ids: list, text: str) -> int:
        """Admin broadcast — DM every given user id, best-effort. Returns how
        many actually got it (blocked-bot / deactivated-account DMs just
        fail silently per-recipient, same as _dm)."""
        sent = 0
        for uid in user_ids:
            if not self._bot:
                continue
            try:
                await self._bot.send_message(chat_id=uid, text=text, parse_mode=ParseMode.HTML)
                sent += 1
            except TelegramAPIError as e:
                logger.warning(f"[broadcast] failed for {uid}: {e}")
        return sent

    # ── Session lookup ──────────────────────────────────────────────────

    def get_or_create(self, user_id: int) -> UserSession:
        session = self._sessions.get(user_id)
        if session is None:
            session = UserSession(user_id=user_id, settings=load_user_settings(user_id))
            self._sessions[user_id] = session
        return session

    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.trading_active)

    # ── Admin visibility ─────────────────────────────────────────────────

    def all_sessions_summary(self) -> list:
        """Snapshot of every session this process has touched since startup
        (in-memory only — a user who hasn't opened the app since the last
        backend restart won't appear here yet). Powers the admin dashboard."""
        rows = []
        for uid, s in self._sessions.items():
            summary = s.risk_manager.get_summary()
            rows.append({
                "user_id": uid,
                "account_mode": s.settings.account_mode,
                "asset": s.settings.get_display_asset(),
                "trading_active": s.trading_active,
                "connecting": s.connecting,
                "connected": s.trader.is_connected,
                "last_error": s.last_error,
                "last_error_detail": s.last_error_detail,
                "session_profit": summary["session_profit"],
                "session_trades": summary["session_trades"],
                "win_rate": summary["win_rate"],
                "created_at": s.created_at.isoformat(),
            })
        rows.sort(key=lambda r: (not r["trading_active"], r["user_id"]))
        return rows

    # ── Start / stop ────────────────────────────────────────────────────

    async def start_trading(self, user_id: int) -> None:
        """async — not because anything else here needs it, but because
        asyncio.create_task() below MUST run on the actual event loop
        thread. This used to be a plain sync method that server.py wrapped
        wholesale in asyncio.to_thread() to keep its one blocking Mongo
        call (the session-limit check) off the event loop — which also
        moved this create_task() call into a worker thread with no running
        loop at all, so every "Start" silently failed to ever schedule
        _run_loop (RuntimeWarning: coroutine '_run_loop' was never
        awaited — sessions looked like they started but nothing ran).
        Thread-wrapping just the one call that actually needs it, here,
        keeps create_task() on the loop where it belongs."""
        session = self.get_or_create(user_id)
        if session.trading_active:
            return
        if self.active_count() >= MAX_CONCURRENT_TRADERS:
            raise RuntimeError(
                f"Server is at capacity ({MAX_CONCURRENT_TRADERS} concurrent traders). "
                "Try again shortly."
            )
        # One check per actual Start press — not per internal reconnect
        # (see _connect(), which no longer checks this itself). This is
        # the number shown to users as their daily session allowance.
        allowed, limit_reason = await asyncio.to_thread(check_and_record_session_start, user_id)
        if not allowed:
            raise RuntimeError(limit_reason)
        session.trading_active = True
        session.last_error = None
        session.last_error_detail = None
        session.live_step_pnl = 0.0
        session.risk_manager.reset()
        session.trader.is_connected = False  # force reconnect w/ current SSID
        session.task = asyncio.create_task(
            self._run_loop(session), name=f"trader-{user_id}"
        )
        self._tlog(session, f"Trading started ({session.settings.account_mode} mode)", "success")
        logger.info(f"▶️ [user {user_id}] Trading started.")

    def stop_trading(self, user_id: int) -> None:
        session = self._sessions.get(user_id)
        if not session:
            return
        session.trading_active = False
        if session.task and not session.task.done():
            session.task.cancel()
        self._tlog(session, "Trading stopped by user", "warn")
        logger.info(f"⏹️ [user {user_id}] Trading stopped.")

    async def stop_all(self) -> None:
        for user_id in list(self._sessions.keys()):
            self.stop_trading(user_id)
        tasks = [s.task for s in self._sessions.values() if s.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Settings persistence ────────────────────────────────────────────

    def save_settings(self, user_id: int) -> None:
        session = self.get_or_create(user_id)
        save_user_settings(user_id, session.settings)

    # ── Per-user trading loop ───────────────────────────────────────────

    async def _connect(
        self, session: UserSession, ssid: str, region_hint: int = 0
    ) -> Tuple[bool, str]:
        """Rate-limited, jittered connect so many users starting at once
        don't all hit PocketOption in the same instant. `connecting` is
        exposed via /api/status so the UI can show a real "Connecting…"
        state instead of guessing from timing.

        The daily session limit is checked once in start_trading(), not
        here — this can run multiple times within one session (internal
        reconnects on a flaky connection), and counting each of those
        against the user's daily allowance was silently burning through it
        far faster than one Start press should. Account protection against
        hammering PocketOption is instead handled by _run_loop capping how
        many reconnects a single session will attempt before giving up.

        region_hint is passed straight through to trader.connect() — see
        its docstring for why alternating this across retries actually
        matters (the library's own URL fallback never triggers for our
        "connects fine, data never arrives" failure mode, so without this
        every retry silently hit the same server).

        Returns (ok, reason) — reason is only meaningful when ok is False."""
        session.connecting = True
        # We know roughly how long this takes — jitter delay, then trader.py's
        # own ASSET_LOAD_WAIT_SECONDS pause plus a short balance-check call —
        # so the UI can show a real countdown instead of an indefinite spinner.
        estimated_seconds = CONNECT_JITTER_MAX_SECONDS + ASSET_LOAD_WAIT_SECONDS + 3
        session.connect_eta = datetime.now(timezone.utc) + timedelta(seconds=estimated_seconds)
        self._tlog(session, f"Connecting to PocketOption ({session.settings.account_mode})…")
        try:
            await asyncio.sleep(random.uniform(0, CONNECT_JITTER_MAX_SECONDS))
            async with self._connect_semaphore:
                ok = await session.trader.connect(ssid, region_hint=region_hint)
            if ok:
                self._tlog(session, "Connected to PocketOption", "success")
                return True, "OK"
            self._tlog(session, "Connect failed", "error")
            return False, "Connection failed. Press Start to try again"
        finally:
            session.connect_eta = None
            session.connecting = False

    async def _run_loop(self, session: UserSession) -> None:
        user_id = session.user_id
        settings = session.settings
        trader = session.trader
        risk_manager = session.risk_manager
        consecutive_errors = 0
        consecutive_stale_balance = 0
        consecutive_connect_failures = 0

        try:
            while session.trading_active:
                try:
                    if not trader.is_connected:
                        ssid = (
                            settings.demo_ssid
                            if settings.account_mode == "DEMO"
                            else settings.real_ssid
                        )
                        if not ssid:
                            session.last_error = "No SSID configured"
                            session.trading_active = False
                            self._tlog(session, "No SSID configured for this mode — stopped", "error")
                            break
                        session.connect_attempt = consecutive_connect_failures + 1
                        session.connect_max_attempts = MAX_CONNECT_RETRIES
                        if settings.account_mode == "DEMO":
                            relay = select_demo_relay(consecutive_connect_failures)
                            if relay:
                                activate_demo_relay(relay)
                        # consecutive_stale_balance/consecutive_connect_failures
                        # double as the region rotation index — 0 on a fresh
                        # Start, incrementing on each retry, so a retry
                        # actually tries a different server instead of the
                        # same one (real accounts only — see trader.connect()).
                        ok, connect_reason = await self._connect(
                            session, ssid, region_hint=consecutive_stale_balance + consecutive_connect_failures
                        )
                        if not ok:
                            consecutive_connect_failures += 1
                            session.last_error = connect_reason
                            if consecutive_connect_failures >= MAX_CONNECT_RETRIES:
                                session.trading_active = False
                                self._tlog(session, f"Stopped: {connect_reason}", "error")
                                await self._dm(user_id, f"⏹️ Trading stopped: {connect_reason}")
                                break
                            wait = min(15 * consecutive_connect_failures, 45)
                            self._tlog(
                                session,
                                f"{connect_reason} — retrying automatically "
                                f"({consecutive_connect_failures}/{MAX_CONNECT_RETRIES}) in {wait:.0f}s…",
                                "warn",
                            )
                            await asyncio.sleep(wait)
                            continue
                        consecutive_connect_failures = 0
                        session.connect_attempt = 0
                        session.connect_max_attempts = 0
                        session.last_error = None
                        session.last_error_detail = None

                    balance = await trader.get_balance()
                    if balance is None:
                        # get_balance() failing means the websocket session
                        # has gone stale/dead even though is_connected is
                        # still True (trader.connect() intentionally treats
                        # "constructed OK" as connected, not "balance()
                        # worked" — see trader.py). Force a real reconnect
                        # rather than crashing below on None formatting or
                        # spinning forever on a zombie connection.
                        consecutive_stale_balance += 1
                        trader.is_connected = False
                        if consecutive_stale_balance >= 3:
                            reason = "Lost connection to PocketOption and couldn't reconnect"
                            session.last_error = reason
                            session.trading_active = False
                            self._tlog(session, f"{reason} — stopped. Press Start to try again.", "error")
                            await self._dm(user_id, f"⏹️ {reason} — stopped. Press Start to try again.")
                            break
                        self._tlog(session, "Lost connection — reconnecting…", "warn")
                        await asyncio.sleep(3)
                        continue
                    consecutive_stale_balance = 0

                    can_trade, reason = risk_manager.can_trade(settings, balance)
                    if not can_trade:
                        session.last_error = reason
                        session.trading_active = False
                        self._tlog(session, reason, "error")
                        await self._dm(user_id, f"⏹️ Trading stopped: {reason}")
                        break

                    asset = settings.asset
                    session.next_trade_at = None
                    self._tlog(session, f"Balance ${balance:.2f} · starting analysis on {settings.get_display_asset()}")
                    self._tlog(session, "Checking RSI, candle patterns & momentum…")
                    await asyncio.sleep(0.4)
                    direction, analysis_detail, is_fallback = await self._strategy.get_direction_verbose(
                        asset, trader.api, settings.direction
                    )
                    self._tlog(session, "Running AI overall analysis…")
                    await asyncio.sleep(0.3)
                    # Only surface genuine indicator output — never the
                    # "data unavailable" fallback text, that reads as a
                    # visible failure even though the bot handles it fine.
                    if not is_fallback:
                        self._tlog(session, analysis_detail)
                    if not direction:
                        self._tlog(session, "No clear direction — skipping this cycle", "warn")
                        await asyncio.sleep(settings.trade_interval)
                        continue

                    cycle_start = datetime.now(timezone.utc)
                    dir_emoji = "📈" if direction.upper() == "CALL" else "📉"
                    self._tlog(
                        session,
                        f"Signal: {settings.get_display_asset()} {direction} "
                        f"${settings.get_martingale_amount(1):.2f} "
                        f"({settings.format_duration(settings.trade_duration)})",
                    )
                    await self._dm(
                        user_id,
                        f"{settings.get_display_asset()}\n"
                        f"{dir_emoji} {direction}\n"
                        f"⏱️ Expiry: {settings.format_duration(settings.trade_duration)}\n"
                        f"💰 Amount: ${settings.get_martingale_amount(1):.2f}",
                    )

                    align_wait = seconds_until_next_candle(
                        settings.trade_duration, settings.timezone_offset
                    )
                    if align_wait > 0.5:
                        self._tlog(session, f"Waiting {align_wait:.0f}s for candle open…")
                        await asyncio.sleep(align_wait)

                    step_labels = {"win": "WIN ✅", "loss": "LOSS ✖️", "doji": "DOJI ⚖️"}
                    step_levels = {"win": "success", "loss": "error", "doji": "warn"}

                    session.live_step_pnl = 0.0  # fresh cycle starting

                    async def _on_step(ev: dict) -> None:
                        label = step_labels.get(ev["result"], ev["result"].upper())
                        level = step_levels.get(ev["result"], "error")
                        idpart = f" · id {ev['trade_id']}" if ev.get("trade_id") else ""
                        self._tlog(
                            session,
                            f"Step {ev['step']}/{ev['max_steps']}: ${ev['amount']:.2f} → {label}{idpart}",
                            level,
                        )
                        # A losing step spends real money right now — reflect
                        # that immediately instead of waiting for the whole
                        # martingale cycle to finish. A win/doji doesn't need
                        # this: the cycle is about to conclude and reset it.
                        if ev["result"] == "loss":
                            session.live_step_pnl -= ev["amount"]

                    result, final_step, total_invested, payout_pct, trade_id, risk_reason = (
                        await trader.execute_martingale(
                            asset=asset, direction=direction, settings=settings,
                            on_step=_on_step,
                            committed_session_profit=risk_manager.session_profit,
                            starting_balance=balance,
                        )
                    )

                    if result == "risk_stop":
                        # A martingale step was about to be placed that
                        # would have breached Stop Loss or Min Balance if it
                        # lost — it was never placed. Book whatever steps
                        # *did* execute before that (if any), then stop for
                        # real instead of drifting into the next cycle, same
                        # as a normal Stop Loss / Min Balance hit.
                        if total_invested > 0:
                            risk_manager.record_loss(
                                asset=asset, direction=direction,
                                total_invested=total_invested, martingale_step=final_step,
                            )
                        session.live_step_pnl = 0.0
                        reason = (
                            f"🛑 Risk protection: next martingale step skipped — {risk_reason} "
                            f"(session: ${risk_manager.session_profit:+.2f})"
                        )
                        session.last_error = reason
                        session.trading_active = False
                        self._tlog(session, reason, "error")
                        await self._dm(user_id, f"⏹️ Trading stopped: {reason}")
                        break

                    if result == "win":
                        risk_manager.record_win(
                            asset=asset,
                            direction=direction,
                            amount=settings.get_martingale_amount(final_step),
                            payout_pct=payout_pct,
                            martingale_step=final_step,
                            total_invested=total_invested,
                        )
                    elif result == "loss":
                        risk_manager.record_loss(
                            asset=asset,
                            direction=direction,
                            total_invested=total_invested,
                            martingale_step=final_step,
                        )
                    elif result == "doji":
                        risk_manager.record_doji(
                            asset=asset,
                            direction=direction,
                            amount=total_invested,
                            martingale_step=final_step,
                        )
                    else:
                        # result is "error" or "invalid_asset" — the *current*
                        # step's own outcome is unknown (never placed, or
                        # placed but its result couldn't be confirmed), so we
                        # deliberately don't count it either way. But if this
                        # was step 2+, every step before it is a *confirmed*
                        # loss (the loop only reaches step N after step N-1
                        # lost) — dropping that from session_profit would
                        # silently understate real losses and let the Stop
                        # Loss gate above keep trading on stale numbers.
                        prior_confirmed_loss = total_invested - settings.get_martingale_amount(final_step)
                        if prior_confirmed_loss > 0:
                            risk_manager.record_loss(
                                asset=asset, direction=direction,
                                total_invested=prior_confirmed_loss, martingale_step=final_step - 1,
                            )
                        risk_manager.record_error(asset, direction)

                    # Cycle is committed now — session_profit already
                    # reflects the true outcome, so the provisional tracker
                    # goes back to zero rather than double-counting.
                    session.live_step_pnl = 0.0

                    consecutive_errors = 0 if result != "error" else consecutive_errors + 1

                    labels = {"win": "WIN ✅", "loss": "LOSS ✖️", "doji": "DOJI ⚖️"}
                    label = labels.get(result, "⚠️ Error")
                    tlog_level = {"win": "success", "loss": "error", "doji": "warn"}.get(result, "error")
                    self._tlog(
                        session,
                        f"Result: {result.upper()} (step {final_step}) — "
                        f"session ${risk_manager.session_profit:+.2f}",
                        tlog_level,
                    )
                    profit_emoji = "🟢" if risk_manager.session_profit >= 0 else "🔴"
                    await self._dm(
                        user_id,
                        f"{label} (step {final_step})\n"
                        f"{profit_emoji} Session: ${risk_manager.session_profit:+.2f} "
                        f"| WR {risk_manager.get_win_rate():.0f}%",
                    )

                    elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
                    wait = max(5.0, settings.trade_interval - elapsed)
                    session.next_trade_at = datetime.now(timezone.utc) + timedelta(seconds=wait)
                    self._tlog(session, f"Next trade in {wait:.0f}s…")
                    await asyncio.sleep(wait)
                    session.next_trade_at = None

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    consecutive_errors += 1
                    friendly = humanize_error(e)
                    session.last_error = friendly
                    session.last_error_detail = f"{type(e).__name__}: {e}"
                    logger.error(f"[user {user_id}] Trading loop error: {e}")
                    logger.error(traceback.format_exc())
                    self._tlog(session, friendly, "error")

                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        logger.error(
                            f"[user {user_id}] {consecutive_errors} consecutive errors — "
                            "stopping this user's session."
                        )
                        stop_msg = "Stopped after repeated issues — your funds and settings are safe. Press Start to try again."
                        self._tlog(session, stop_msg, "error")
                        session.trading_active = False
                        await self._dm(user_id, f"⏹️ Trading stopped: {stop_msg}")
                        break

                    wait = min(30 * (2 ** min(consecutive_errors - 1, 3)), 120)
                    self._tlog(session, f"Retrying in {wait:.0f}s…", "warn")
                    await asyncio.sleep(wait)

        except asyncio.CancelledError:
            pass
        finally:
            session.trading_active = False
            session.next_trade_at = None
            session.live_step_pnl = 0.0
            if risk_manager.session_trades > 0:
                append_session(user_id, {
                    "started_at": risk_manager.session_start.isoformat(),
                    "stopped_at": datetime.now(timezone.utc).isoformat(),
                    "account_mode": settings.account_mode,
                    "asset": settings.get_display_asset(),
                    "summary": risk_manager.get_summary(),
                    "trades": [
                        {
                            "asset": t.asset,
                            "direction": t.direction,
                            "amount": t.amount,
                            "result": t.result,
                            "profit": t.profit,
                            "payout_pct": t.payout_pct,
                            "martingale_step": t.martingale_step,
                            "timestamp": t.timestamp.isoformat(),
                        }
                        for t in risk_manager.trade_history
                    ],
                })
                self._tlog(
                    session,
                    f"Session archived — {risk_manager.session_trades} trades, "
                    f"${risk_manager.session_profit:+.2f}",
                )
            logger.info(f"[user {user_id}] Trading loop exited.")
