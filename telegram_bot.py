"""
Telegram Bot — /start entry point into the Mini App.

All trading setup and control happens in the Mini App (see server.py /
frontend); this bot only greets the user with a button into it. Trade
signals/results are DMed straight to each trading user by
session_manager.py — no channel or bot-side broadcasting involved.
"""

from __future__ import annotations

import logging
import socket

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.enums import ParseMode

from config import MINIAPP_URL, TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Keyboard builders
# ═══════════════════════════════════════════════════════════════════════════


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_menu_kb_public() -> InlineKeyboardMarkup:
    """Every user (admin included) just gets a button into their own Mini
    App trading session — all setup and control happens there now."""
    if MINIAPP_URL:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📱 Open Trading App", web_app=WebAppInfo(url=MINIAPP_URL))
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[])


# ═══════════════════════════════════════════════════════════════════════════
# Telegram Bot Controller
# ═══════════════════════════════════════════════════════════════════════════


class TelegramBotController:
    """Manages the Telegram bot: just the /start entry point into the Mini
    App. All trading setup and control happens there, per-user."""

    def __init__(self):
        # Default aiohttp session timeout (60s) leaves little headroom over
        # the long-poll wait itself once DNS/TLS/network latency is added —
        # seen this VPS's connection to Telegram's API spuriously time out
        # and have to reconnect (self-recovering, but delays whatever the
        # user just sent by several seconds). More headroom here means
        # fewer of those retries.
        #
        # Root cause of those timeouts: api.telegram.org resolves to an
        # IPv6 address here, and this VPS's IPv6 routing is broken (hangs
        # outright rather than failing fast) while IPv4 to the same host
        # works fine. Forcing IPv4 via the connector avoids ever hitting
        # the broken path instead of just tolerating it with a longer
        # timeout. AiohttpSession doesn't expose a constructor param for
        # this — it builds its aiohttp.TCPConnector kwargs internally — so
        # set it directly on the (plain dict) attribute before first use.
        session = AiohttpSession(timeout=90)
        session._connector_init["family"] = socket.AF_INET
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN, session=session)
        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)

        self._register_handlers()

    # ─────────────────────────────────────────────────────────────────────
    # Handler registration
    # ─────────────────────────────────────────────────────────────────────

    def _register_handlers(self):
        r = self.router
        r.message.register(self._cmd_start, CommandStart())

    # ─────────────────────────────────────────────────────────────────────
    # Command handlers
    # ─────────────────────────────────────────────────────────────────────

    async def _cmd_start(self, message: Message, state: FSMContext):
        await state.clear()
        await message.answer(
            "👋 <b>Welcome to Auto Trader</b>\n\n"
            "Open the app below to set up your own PocketOption SSID "
            "and start trading — every trader gets their own isolated session.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb_public(),
        )

    # ─────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────

    async def start_polling(self) -> None:
        """Start the Telegram bot polling loop."""
        await self._register_commands()
        logger.info("Starting Telegram bot polling...")
        await self.dp.start_polling(self.bot)

    async def _register_commands(self) -> None:
        """Register the '/' command menu with Telegram (BotFather setMyCommands).
        Without this, typing '/' shows no autocomplete at all — commands still
        work if typed by hand, but look broken to anyone expecting the menu."""
        from aiogram.types import BotCommand

        try:
            await self.bot.set_my_commands([
                BotCommand(command="start", description="Open the trading app"),
            ])
        except Exception as e:
            logger.warning(f"Failed to register bot commands: {e}")

    async def stop(self) -> None:
        """Stop the bot and close session."""
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        try:
            await self.bot.session.close()
        except Exception:
            pass
