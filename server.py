"""
Mini App backend — standalone JSON API, multi-user.

Every request is identified by its verified Telegram user (see auth.py),
and every endpoint operates on *that user's own* isolated session from
session_manager.py — their own settings, SSID, trader connection, and
risk manager. Users never share state, so one user's connection issues
can't stall another user's trading.

Serves JSON only — the frontend (in ../frontend) is deployed separately
(e.g. to Vercel) and talks to this API over HTTPS/CORS.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth import require_admin, require_user
from config import FRONTEND_ORIGINS, HIDDEN_ADMIN_USER_IDS, MAX_CONCURRENT_TRADERS, TELEGRAM_ADMIN_IDS
from connect_limit_store import get_connect_usage_today
from permissions_store import (
    demo_subscription_status,
    grant_demo_trading,
    grant_real_trading,
    is_demo_trading_enabled,
    is_real_trading_enabled,
    load_permissions,
    revoke_demo_trading,
    revoke_real_trading,
    subscription_status,
    subscription_status_bulk_both,
)
from insights import compute_insights
from payment_store import load_payments_bulk, payment_totals, set_payment
from profile_store import display_names_bulk, load_profiles_bulk
from session_history_store import load_session_history, load_session_history_bulk
from session_manager import SessionManager
from user_store import list_user_ids, load_user_settings_bulk

_IS_DEMO_RE = re.compile(r'"isDemo"\s*:\s*(\d)')


def _ssid_is_demo(ssid: str) -> Optional[bool]:
    """PocketOption's SSID payload always carries its own real/demo flag —
    trust that, never the UI field a value happened to be pasted into.
    Returns None if the string doesn't look like a valid SSID at all."""
    m = _IS_DEMO_RE.search(ssid)
    if not m:
        return None
    return m.group(1) == "1"

logger = logging.getLogger(__name__)

# Local-dev convenience only: if a sibling ../frontend checkout exists, serve
# it from this same process so a single ngrok tunnel can expose both the API
# and the UI. Not used in production — Vercel serves the real frontend build
# independently and this backend never ships a frontend/ directory there.
_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

COMMON_ASSETS = [
    # Commodities
    "XAUUSD_otc", "XAGUSD_otc", "UKBrent_otc", "USCrude_otc",
    # Majors
    "EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "USDCHF_otc",
    "USDCAD_otc", "AUDUSD_otc", "NZDUSD_otc",
    # EUR crosses
    "EURGBP_otc", "EURJPY_otc", "EURCHF_otc", "EURCAD_otc",
    "EURAUD_otc", "EURNZD_otc",
    # GBP crosses
    "GBPJPY_otc", "GBPCHF_otc", "GBPCAD_otc", "GBPAUD_otc", "GBPNZD_otc",
    # AUD / NZD / CAD / CHF crosses
    "AUDJPY_otc", "AUDCHF_otc", "AUDCAD_otc", "AUDNZD_otc",
    "CADJPY_otc", "CADCHF_otc", "CHFJPY_otc",
    "NZDJPY_otc", "NZDCAD_otc", "NZDCHF_otc",
    # USD exotics
    "USDTRY_otc", "USDZAR_otc", "USDMXN_otc", "USDSGD_otc",
    "USDHKD_otc", "USDSEK_otc", "USDNOK_otc", "USDPLN_otc",
]


class SettingsUpdate(BaseModel):
    account_mode: Optional[str] = None
    asset: Optional[str] = None
    direction: Optional[str] = None
    base_amount: Optional[float] = None
    trade_duration: Optional[int] = None
    trade_interval: Optional[int] = None
    martingale_enabled: Optional[bool] = None
    martingale_max_steps: Optional[int] = None
    martingale_multiplier: Optional[float] = None
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    max_consecutive_losses: Optional[int] = None
    max_trades: Optional[int] = None
    min_balance: Optional[float] = None
    demo_ssid: Optional[str] = None
    real_ssid: Optional[str] = None


class PermissionUpdate(BaseModel):
    # "real" or "demo" — which subscription track this grant/revoke applies to.
    mode: str = "real"
    trading_enabled: bool
    # Required when trading_enabled is true: "monthly" (auto-expires in
    # 30 days) or "lifetime" (never expires). Ignored when revoking access.
    subscription_type: Optional[str] = None
    # Optional payment info recorded alongside a grant (ignored on revoke) —
    # see payment_store.py. Both omitted leaves any existing payment record
    # untouched rather than resetting it to $0/unpaid.
    payment_amount: Optional[float] = None
    paid: Optional[bool] = None


class PaymentUpdate(BaseModel):
    amount: float
    paid: bool


class BroadcastRequest(BaseModel):
    message: str


def create_app(manager: SessionManager) -> FastAPI:
    # docs_url/redoc_url/openapi_url disabled: FastAPI serves these
    # publicly by default, which hands anyone who finds this server a full
    # map of every endpoint (including admin routes) and its parameters —
    # pure reconnaissance for an attacker, no reason to expose it.
    app = FastAPI(title="Auto Trader Mini App API", version="2.0.0", docs_url=None, redoc_url=None, openapi_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=FRONTEND_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Telegram-Init-Data", "X-Admin-Token"],
    )

    @app.middleware("http")
    async def _no_cache(request, call_next):
        """Telegram's in-app WebView can cache aggressively without this —
        we were shipping UI fixes that testers never actually saw because
        their client kept serving a stale index.html/app.js/style.css.
        This app iterates too fast for any client-side caching to be safe."""
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    def _settings_dict(session, user_id: int) -> Dict[str, Any]:
        data = asdict(session.settings)
        # Never ship raw SSIDs to the client — just whether they're set.
        data["demo_ssid_set"] = bool(data.pop("demo_ssid", ""))
        data["real_ssid_set"] = bool(data.pop("real_ssid", ""))
        sub = subscription_status(user_id)
        data["real_trading_enabled"] = sub["active"]
        data["subscription_type"] = sub["subscription_type"]
        data["subscription_remaining_seconds"] = sub["remaining_seconds"]
        demo_sub = demo_subscription_status(user_id)
        data["demo_trading_enabled"] = demo_sub["active"]
        data["demo_subscription_type"] = demo_sub["subscription_type"]
        data["demo_subscription_remaining_seconds"] = demo_sub["remaining_seconds"]
        return data

    @app.get("/api/status")
    async def status(user=Depends(require_user)):
        session = manager.get_or_create(user["id"])
        balance = None
        if session.trader.is_connected:
            try:
                balance = await session.trader.get_balance()
            except Exception:
                balance = None
        # get_connect_usage_today does a blocking pymongo call — running it
        # inline here would block the *entire* single-threaded event loop
        # (not just this request) for however long that Mongo round trip
        # takes, stalling every other concurrent user's request too. This
        # endpoint is polled every 3s by every open Mini App, so it was the
        # single most frequent place that could happen.
        connects_used, connects_limit = await asyncio.to_thread(
            get_connect_usage_today, user["id"]
        )
        return {
            "is_admin": bool(TELEGRAM_ADMIN_IDS) and user["id"] in TELEGRAM_ADMIN_IDS,
            "trading_active": session.trading_active,
            "connecting": session.connecting,
            "connect_eta": session.connect_eta.isoformat() if session.connect_eta else None,
            "connect_attempt": session.connect_attempt,
            "connect_max_attempts": session.connect_max_attempts,
            "connected": session.trader.is_connected,
            "account_mode": session.settings.account_mode,
            "asset": session.settings.get_display_asset(),
            "balance": balance,
            "last_error": session.last_error,
            "next_trade_at": session.next_trade_at.isoformat() if session.next_trade_at else None,
            "take_profit": session.settings.take_profit,
            "stop_loss": session.settings.stop_loss,
            "max_trades": session.settings.max_trades,
            "live_step_pnl": session.live_step_pnl,
            "session": session.risk_manager.get_summary(),
            "session_duration": session.risk_manager.get_session_duration(),
            "connects_used_today": connects_used,
            "connects_limit_today": connects_limit,
            "connects_remaining_today": max(0, connects_limit - connects_used),
            "server_load": {
                "active_traders": manager.active_count(),
                "capacity": MAX_CONCURRENT_TRADERS,
            },
        }

    # _settings_dict() does two blocking pymongo reads (subscription_status
    # + demo_subscription_status) — same event-loop-blocking risk as the
    # admin endpoints above, and this one is what "Could not load settings"
    # was almost certainly hitting, since it runs on every settings load.
    @app.get("/api/settings")
    async def get_settings(user=Depends(require_user)):
        session = manager.get_or_create(user["id"])
        return await asyncio.to_thread(_settings_dict, session, user["id"])

    def _update_settings_sync(session, user_id: int, updates: dict) -> Dict[str, Any]:
        # SSIDs carry their own real/demo flag ("isDemo":0 or 1) — verify
        # it against whichever field the value is being saved into, rather
        # than trusting the field name alone. Without this, someone could
        # paste a REAL-money SSID into the Demo field to dodge the
        # real-trading permission check below entirely.
        if "demo_ssid" in updates:
            is_demo = _ssid_is_demo(updates["demo_ssid"])
            if is_demo is False:
                raise HTTPException(
                    400,
                    "That looks like a REAL account SSID (isDemo:0) — it can't go in the "
                    "Demo SSID field. Switch to Real mode and paste it there instead.",
                )
            if is_demo is None:
                raise HTTPException(400, "That doesn't look like a valid PocketOption SSID.")
            if not is_demo_trading_enabled(user_id):
                raise HTTPException(
                    403,
                    "Demo trading isn't enabled for your account yet — ask an admin to turn it on.",
                )

        if "real_ssid" in updates:
            is_demo = _ssid_is_demo(updates["real_ssid"])
            if is_demo is True:
                raise HTTPException(
                    400,
                    "That looks like a DEMO account SSID (isDemo:1) — it can't go in the "
                    "Real SSID field. Switch to Demo mode and paste it there instead.",
                )
            if is_demo is None:
                raise HTTPException(400, "That doesn't look like a valid PocketOption SSID.")
            if not is_real_trading_enabled(user_id):
                raise HTTPException(
                    403,
                    "Real-money trading isn't enabled for your account yet — ask an admin to turn it on.",
                )
            if not session.settings.real_risk_ack:
                raise HTTPException(
                    403,
                    "Please review and accept the real-money risk disclosure first (shown when you switch to Real mode).",
                )

        for key, value in updates.items():
            setattr(session.settings, key, value)

        is_valid, errors = session.settings.validate()
        if not is_valid:
            raise HTTPException(400, "; ".join(errors))

        manager.save_settings(user_id)
        # Force reconnect if account mode or SSIDs changed
        if "account_mode" in updates or "demo_ssid" in updates or "real_ssid" in updates:
            session.trader.is_connected = False
        return _settings_dict(session, user_id)

    @app.post("/api/settings")
    async def update_settings(body: SettingsUpdate, user=Depends(require_user)):
        session = manager.get_or_create(user["id"])
        updates = {k: v for k, v in body.dict().items() if v is not None}
        if not updates:
            raise HTTPException(400, "No fields to update")
        return await asyncio.to_thread(_update_settings_sync, session, user["id"], updates)

    def _risk_ack_sync(session, user_id: int) -> Dict[str, Any]:
        session.settings.real_risk_ack = True
        session.settings.real_risk_ack_at = datetime.now(timezone.utc).isoformat()
        manager.save_settings(user_id)
        return _settings_dict(session, user_id)

    @app.post("/api/risk-ack")
    async def risk_ack(user=Depends(require_user)):
        """Records that this user has clicked through the real-money risk
        disclosure — required once before a real_ssid can be saved or
        REAL-mode trading started (see the checks in _update_settings_sync
        and _check_start_permissions_sync). The frontend shows the actual
        disclosure text and only calls this after the customer accepts it."""
        session = manager.get_or_create(user["id"])
        return await asyncio.to_thread(_risk_ack_sync, session, user["id"])

    @app.get("/api/assets")
    async def assets(user=Depends(require_user)):
        return {"assets": COMMON_ASSETS}

    @app.get("/api/payout")
    async def payout(asset: str, user=Depends(require_user)):
        """Current payout % for an asset — fetched on demand (asset change,
        Settings tab open) rather than on every status poll, since it's a
        live call to PocketOption and we don't want to hammer it every 3s."""
        session = manager.get_or_create(user["id"])
        if not session.trader.is_connected:
            return {"payout_pct": None}
        try:
            pct = await session.trader.get_payout(session.trader.normalize_asset(asset))
        except Exception:
            pct = None
        return {"payout_pct": pct}

    def _check_start_permissions_sync(session, user_id: int) -> None:
        """Pure blocking-call validation (Mongo permission reads) — safe to
        run in a thread since it never touches asyncio task/loop objects.
        Raises HTTPException on failure; returns nothing on success."""
        ssid = (
            session.settings.demo_ssid
            if session.settings.account_mode == "DEMO"
            else session.settings.real_ssid
        )
        if not ssid:
            raise HTTPException(400, "No SSID configured for the current account mode")
        # Re-check here too, not just on save — a permission could have been
        # revoked after the SSID was stored, and this is the point actual
        # money starts moving.
        if session.settings.account_mode == "REAL" and not is_real_trading_enabled(user_id):
            raise HTTPException(
                403,
                "Real-money trading isn't enabled for your account yet — ask an admin to turn it on.",
            )
        if session.settings.account_mode == "REAL" and not session.settings.real_risk_ack:
            raise HTTPException(
                403,
                "Please review and accept the real-money risk disclosure first (shown when you switch to Real mode).",
            )
        if session.settings.account_mode == "DEMO" and not is_demo_trading_enabled(user_id):
            raise HTTPException(
                403,
                "Demo trading isn't enabled for your account yet — ask an admin to turn it on.",
            )

    # This is the literal Start button. The permission checks are blocking
    # Mongo calls — thread-wrapped like everywhere else above — but
    # manager.start_trading() itself is awaited directly here, on this
    # event loop, never inside a thread: it calls asyncio.create_task() to
    # spawn the trading loop, which silently does nothing (coroutine never
    # awaited, session looks "started" but nothing runs) if called from a
    # thread with no running loop. See start_trading()'s own docstring.
    @app.post("/api/start")
    async def start_trading(user=Depends(require_user)):
        session = manager.get_or_create(user["id"])
        await asyncio.to_thread(_check_start_permissions_sync, session, user["id"])
        try:
            await manager.start_trading(user["id"])
        except RuntimeError as e:
            raise HTTPException(429, str(e))
        return {"trading_active": True}

    @app.post("/api/stop")
    async def stop_trading(user=Depends(require_user)):
        manager.stop_trading(user["id"])
        return {"trading_active": False}

    @app.get("/api/history")
    async def history(limit: int = 50, user=Depends(require_user)):
        session = manager.get_or_create(user["id"])
        records = session.risk_manager.trade_history[-limit:][::-1]
        return {
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
                for t in records
            ]
        }

    @app.get("/api/logs")
    async def logs(after: int = 0, user=Depends(require_user)):
        """Live event feed for the Terminal tab. `after` is the index of the
        last log line the client already has — only newer lines are sent,
        so polling stays cheap even with a long-running session."""
        session = manager.get_or_create(user["id"])
        all_logs = list(session.logs)
        new_logs = all_logs[after:] if after < len(all_logs) else []
        return {"logs": new_logs, "next_after": len(all_logs)}

    @app.get("/api/sessions")
    async def sessions(limit: int = 20, user=Depends(require_user)):
        """Past completed trading sessions (Start-to-Stop runs), most recent
        first, each carrying its own trade list — this is what the History
        tab's expandable session cards render."""
        history = await asyncio.to_thread(load_session_history, user["id"])
        return {"sessions": history[:limit]}

    def _export_csv_sync(session, user_id: int) -> str:
        rows = [
            {
                "timestamp": t.timestamp.isoformat(),
                "account_mode": session.settings.account_mode,
                "asset": t.asset,
                "direction": t.direction,
                "amount": t.amount,
                "martingale_step": t.martingale_step,
                "result": t.result,
                "payout_pct": t.payout_pct,
                "profit": t.profit,
            }
            for t in session.risk_manager.trade_history
        ]
        for sess in load_session_history(user_id):
            for t in sess.get("trades", []):
                rows.append({
                    "timestamp": t["timestamp"],
                    "account_mode": sess.get("account_mode", ""),
                    "asset": t["asset"],
                    "direction": t["direction"],
                    "amount": t["amount"],
                    "martingale_step": t["martingale_step"],
                    "result": t["result"],
                    "payout_pct": t["payout_pct"],
                    "profit": t["profit"],
                })
        rows.sort(key=lambda r: r["timestamp"], reverse=True)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Timestamp", "Account", "Asset", "Direction", "Amount", "Martingale Step", "Result", "Payout %", "Profit"])
        for r in rows:
            writer.writerow([
                r["timestamp"], r["account_mode"], r["asset"], r["direction"],
                r["amount"], r["martingale_step"], r["result"], r["payout_pct"], r["profit"],
            ])
        return buf.getvalue()

    @app.post("/api/history/export")
    async def export_history_csv(user=Depends(require_user)):
        """Full trade history (current live session + every archived
        session), sent as a Telegram document straight into the customer's
        own chat with the bot — not a downloadable link. A link opened in
        the system browser would show the customer this server's real
        address (see the sslip.io / IP-exposure issue); a bot-delivered
        file needs no public URL and never leaves Telegram's own
        infrastructure."""
        session = manager.get_or_create(user["id"])
        csv_text = await asyncio.to_thread(_export_csv_sync, session, user["id"])
        filename = f"trade-history-{datetime.now(timezone.utc).date().isoformat()}.csv"
        sent = await manager.send_document(
            user["id"], filename, csv_text.encode("utf-8"),
            caption="📄 Your trade history export.",
        )
        if not sent:
            raise HTTPException(
                502,
                "Couldn't send the file — open a chat with the bot first (send it /start), then try again.",
            )
        return {"sent": True}

    def _insights_sync() -> dict:
        all_trades = []
        for sessions in load_session_history_bulk(list_user_ids()).values():
            for sess in sessions:
                all_trades.extend(sess.get("trades", []))
        return compute_insights(all_trades)

    @app.get("/api/insights")
    async def insights(user=Depends(require_user)):
        """Platform-wide (every user, demo+real combined) win-rate patterns
        by pair/hour/day/martingale-step — powers the Insights tab. Every
        customer sees the same aggregate numbers; nothing here is scoped to
        or reveals any individual user's data."""
        return await asyncio.to_thread(_insights_sync)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "active_traders": manager.active_count()}

    # ── Admin ────────────────────────────────────────────────────────────
    # Separate from every endpoint above: these aggregate ACROSS users
    # rather than scoping to the caller's own session, so they're gated by
    # require_admin (checks TELEGRAM_ADMIN_IDS), not just require_user.
    #
    # Deliberately absent: any way for an admin to START trading on a
    # user's behalf. Admins can see everything, grant/revoke real-trading
    # access, and emergency-stop — never place a trade for someone else.

    _hidden_admin_ids = set(HIDDEN_ADMIN_USER_IDS)

    def _visible_user_ids(user_ids: list) -> list:
        """Every admin listing (Users, Subscriptions, Sessions, Leaderboard)
        goes through this — see HIDDEN_ADMIN_USER_IDS in config.py. Their
        own trading access is untouched; this only controls what other
        admins see in these four views."""
        if not _hidden_admin_ids:
            return user_ids
        return [uid for uid in user_ids if uid not in _hidden_admin_ids]

    def _visible_rows(rows: list) -> list:
        """Same filter as _visible_user_ids(), for row-dicts keyed by
        "user_id" instead of a bare id list (e.g. live session summaries)."""
        if not _hidden_admin_ids:
            return rows
        return [r for r in rows if r["user_id"] not in _hidden_admin_ids]

    def _lifetime_stats_from_sessions(sessions: list) -> dict:
        """Aggregate one user's archived sessions (see session_history_store)
        into all-time totals — the live in-memory session only covers the
        current run since the backend last restarted. Takes the sessions
        list directly (rather than a user id + its own DB call) so admin
        routes can fetch every user's history in one bulk query instead of
        one blocking round trip per user — with many users, doing this per
        user (and the same for settings/profile/permissions lookups
        alongside it) was slow enough to stall the whole API for everyone
        else while an admin's dashboard loaded, since the Mongo client here
        is synchronous and every call ran straight on the event loop."""
        trades = wins = losses = dojis = 0
        profit = 0.0
        for sess in sessions:
            s = sess.get("summary", {})
            trades += s.get("session_trades", 0)
            wins += s.get("wins", 0)
            losses += s.get("losses", 0)
            dojis += s.get("dojis", 0)
            profit += s.get("session_profit", 0.0)
        return {
            "lifetime_trades": trades,
            "lifetime_wins": wins,
            "lifetime_losses": losses,
            "lifetime_dojis": dojis,
            "lifetime_profit": round(profit, 2),
            "lifetime_win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0.0,
        }

    def _admin_overview_sync() -> dict:
        live_rows = _visible_rows(manager.all_sessions_summary())
        user_ids = _visible_user_ids(list_user_ids())
        history_by_id = load_session_history_bulk(user_ids)
        lifetime = [_lifetime_stats_from_sessions(history_by_id[uid]) for uid in user_ids]
        total_trades = sum(l["lifetime_trades"] for l in lifetime)
        total_wins = sum(l["lifetime_wins"] for l in lifetime)
        total_losses = sum(l["lifetime_losses"] for l in lifetime)
        real_status, demo_status = subscription_status_bulk_both(user_ids)
        real_enabled = sum(1 for s in real_status.values() if s["active"])
        demo_enabled = sum(1 for s in demo_status.values() if s["active"])
        # Only users with currently-active access owe anything — everyone
        # else was never sold a grant to collect payment for.
        paying_ids = [uid for uid in user_ids if real_status[uid]["active"] or demo_status[uid]["active"]]
        payments = payment_totals(paying_ids)

        return {
            "active_traders": manager.active_count(),
            "capacity": MAX_CONCURRENT_TRADERS,
            "demo_active": sum(1 for r in live_rows if r["trading_active"] and r["account_mode"] == "DEMO"),
            "real_active": sum(1 for r in live_rows if r["trading_active"] and r["account_mode"] == "REAL"),
            "total_registered_users": len(user_ids),
            "real_trading_enabled_users": real_enabled,
            "demo_trading_enabled_users": demo_enabled,
            "total_session_profit": round(sum(r["session_profit"] for r in live_rows), 2),
            "lifetime_trades": total_trades,
            "lifetime_profit": round(sum(l["lifetime_profit"] for l in lifetime), 2),
            "lifetime_win_rate": round(total_wins / (total_wins + total_losses) * 100, 1) if (total_wins + total_losses) else 0.0,
            "total_paid": payments["total_paid"],
            "pending_amount": payments["pending_amount"],
            "not_paid_count": payments["not_paid_count"],
        }

    # Every /api/admin/* handler below is pure synchronous logic — several
    # blocking pymongo calls each, called directly inside an async def.
    # That doesn't just block *that* request: it blocks Python's entire
    # single-threaded event loop for however long Mongo takes to respond,
    # stalling every other concurrent user's request too (this is what was
    # causing /api/status and Stop to intermittently time out — see
    # trader.py's history for the same class of bug). These are polled
    # every 4s by every open admin panel, so it's the same risk as
    # /api/status, just for admins. asyncio.to_thread() moves the blocking
    # work off the event loop entirely.
    @app.get("/api/admin/overview")
    async def admin_overview(_=Depends(require_admin)):
        return await asyncio.to_thread(_admin_overview_sync)

    def _admin_sessions_sync() -> dict:
        rows = _visible_rows(manager.all_sessions_summary())
        names = display_names_bulk([r["user_id"] for r in rows])
        for r in rows:
            r["name"] = names[r["user_id"]]
        return {"sessions": rows}

    @app.get("/api/admin/sessions")
    async def admin_sessions(_=Depends(require_admin)):
        return await asyncio.to_thread(_admin_sessions_sync)

    @app.post("/api/admin/sessions/{target_user_id}/stop")
    async def admin_stop_session(target_user_id: int, _=Depends(require_admin)):
        manager.stop_trading(target_user_id)
        return {"stopped": target_user_id}

    def _admin_users_sync() -> dict:
        """Every user who has ever saved settings — not just ones with a
        live in-memory session — so the admin can grant real-trading access
        to someone before they've even opened the app today."""
        live_by_id = {r["user_id"]: r for r in manager.all_sessions_summary()}
        user_ids = _visible_user_ids(list_user_ids())
        settings_by_id = load_user_settings_bulk(user_ids)
        profiles_by_id = load_profiles_bulk(user_ids)
        names = display_names_bulk(user_ids)
        real_status, demo_status = subscription_status_bulk_both(user_ids)
        history_by_id = load_session_history_bulk(user_ids)
        payments_by_id = load_payments_bulk(user_ids)
        rows = []
        for uid in user_ids:
            settings = settings_by_id[uid]
            profile = profiles_by_id.get(uid)
            rows.append({
                "user_id": uid,
                "name": names[uid],
                "is_premium_telegram": bool(profile and profile.get("is_premium")),
                "account_mode": settings.account_mode,
                "demo_ssid_set": bool(settings.demo_ssid),
                "real_ssid_set": bool(settings.real_ssid),
                "subscription": real_status[uid],
                "demo_subscription": demo_status[uid],
                "live": live_by_id.get(uid),
                "payment": payments_by_id[uid],
                **_lifetime_stats_from_sessions(history_by_id[uid]),
            })
        rows.sort(key=lambda r: (not (r["live"] and r["live"]["trading_active"]), -r["lifetime_profit"]))
        return {"users": rows}

    @app.get("/api/admin/users")
    async def admin_users(_=Depends(require_admin)):
        return await asyncio.to_thread(_admin_users_sync)

    def _admin_set_permissions_sync(
        target_user_id: int, body: PermissionUpdate, admin_id: int
    ) -> tuple:
        """Pure blocking-call logic (Mongo grant/revoke + status read) —
        safe to thread. Does NOT touch manager.stop_trading()/session.task:
        Task.cancel() isn't documented as thread-safe, and doing it from a
        worker thread is the same class of bug as start_trading()'s
        create_task() was — see that method's docstring. The revoke case
        signals "should we also stop them" back to the caller, which does
        the actual stop on the main loop."""
        mode = body.mode if body.mode in ("real", "demo") else "real"
        grant_fn = grant_real_trading if mode == "real" else grant_demo_trading
        revoke_fn = revoke_real_trading if mode == "real" else revoke_demo_trading
        status_fn = subscription_status if mode == "real" else demo_subscription_status

        if body.trading_enabled:
            if body.subscription_type not in ("monthly", "lifetime"):
                raise HTTPException(400, "subscription_type must be 'monthly' or 'lifetime'")
            grant_fn(target_user_id, body.subscription_type, admin_id)
            if body.payment_amount is not None or body.paid is not None:
                set_payment(
                    target_user_id,
                    body.payment_amount if body.payment_amount is not None else 0.0,
                    bool(body.paid),
                    admin_id,
                )
        else:
            revoke_fn(target_user_id, admin_id)
        return status_fn(target_user_id), mode

    @app.post("/api/admin/users/{target_user_id}/permissions")
    async def admin_set_permissions(
        target_user_id: int, body: PermissionUpdate, admin=Depends(require_admin)
    ):
        status, mode = await asyncio.to_thread(
            _admin_set_permissions_sync, target_user_id, body, admin["id"]
        )
        if not body.trading_enabled:
            # Immediately stop them if they're actively trading in this
            # mode right now, rather than waiting for their session to
            # end — done here, directly on the event loop.
            session = manager.get_or_create(target_user_id)
            if session.trading_active and session.settings.account_mode == mode.upper():
                manager.stop_trading(target_user_id)
        return status

    @app.post("/api/admin/users/{target_user_id}/payment")
    async def admin_set_payment(
        target_user_id: int, body: PaymentUpdate, admin=Depends(require_admin)
    ):
        """Standalone payment edit — independent of granting/revoking
        access, so an admin can correct or update a payment record (e.g.
        this week's settlement finally came in) without also having to
        touch that user's trading access."""
        return await asyncio.to_thread(set_payment, target_user_id, body.amount, body.paid, admin["id"])

    def _admin_subscriptions_sync() -> dict:
        """Dedicated view of trading access grants — both the real-money
        and demo tracks — active monthly (soonest-expiring first), then
        lifetime, then lapsed/revoked ones an admin might want to renew.
        Users who were never granted access on a given track don't show
        up here for that track."""
        user_ids = _visible_user_ids(list_user_ids())
        names = display_names_bulk(user_ids)
        real_status, demo_status = subscription_status_bulk_both(user_ids)
        rows = []
        for uid in user_ids:
            for mode, status_by_id in (("real", real_status), ("demo", demo_status)):
                sub = status_by_id[uid]
                if sub["subscription_type"] is None:
                    continue
                rows.append({"user_id": uid, "name": names[uid], "mode": mode, **sub})

        def sort_key(r):
            if r["active"] and r["subscription_type"] == "monthly":
                return (0, r["remaining_seconds"])
            if r["active"] and r["subscription_type"] == "lifetime":
                return (1, 0)
            return (2, 0)

        rows.sort(key=sort_key)
        return {"subscriptions": rows}

    @app.get("/api/admin/subscriptions")
    async def admin_subscriptions(_=Depends(require_admin)):
        return await asyncio.to_thread(_admin_subscriptions_sync)

    def _admin_leaderboard_sync(limit: int) -> dict:
        user_ids = _visible_user_ids(list_user_ids())
        names = display_names_bulk(user_ids)
        history_by_id = load_session_history_bulk(user_ids)
        rows = []
        for uid in user_ids:
            stats = _lifetime_stats_from_sessions(history_by_id[uid])
            if stats["lifetime_trades"] == 0:
                continue
            rows.append({"user_id": uid, "name": names[uid], **stats})
        rows.sort(key=lambda r: r["lifetime_profit"], reverse=True)
        return {"leaderboard": rows[:limit]}

    @app.get("/api/admin/leaderboard")
    async def admin_leaderboard(limit: int = 10, _=Depends(require_admin)):
        return await asyncio.to_thread(_admin_leaderboard_sync, limit)

    def _admin_equity_sync(limit: int) -> dict:
        """Platform-wide cumulative P&L curve — every archived trade from
        every user, flattened and sorted chronologically. Bounded to the
        most recent `limit` trades so this stays cheap regardless of how
        much history has piled up."""
        all_trades = []
        for sessions in load_session_history_bulk(list_user_ids()).values():
            for sess in sessions:
                all_trades.extend(sess.get("trades", []))
        all_trades.sort(key=lambda t: t["timestamp"])
        recent = all_trades[-limit:]
        running = 0.0
        points = []
        for t in recent:
            running += t.get("profit", 0.0)
            points.append({"ts": t["timestamp"], "cum_profit": round(running, 2)})
        return {"points": points}

    @app.get("/api/admin/equity")
    async def admin_equity(limit: int = 100, _=Depends(require_admin)):
        return await asyncio.to_thread(_admin_equity_sync, limit)

    @app.post("/api/admin/broadcast")
    async def admin_broadcast(body: BroadcastRequest, _=Depends(require_admin)):
        text = body.message.strip()
        if not text:
            raise HTTPException(400, "Message can't be empty")
        user_ids = await asyncio.to_thread(list_user_ids)
        sent = await manager.broadcast(user_ids, text)
        return {"sent": sent, "total": len(user_ids)}

    if os.path.isdir(_FRONTEND_DIR):
        app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend-dev")
        logger.info(f"Serving local frontend from {_FRONTEND_DIR} (dev convenience only)")

    return app
