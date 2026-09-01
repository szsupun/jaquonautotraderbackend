"""
Auto Trader Bot — Main Entry Point.

Runs two concurrent tasks:
  1. Telegram bot (aiogram polling) — just the /start entry point into the
     Mini App now; all trading setup and control happens there.
  2. Mini App REST API (multi-user) — every Telegram user who opens the app
     gets their own isolated settings/SSID/trader/loop (see session_manager.py).
     Trade signals/results are DMed straight to that user — no shared channel.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback

from config import LOG_FILE, LOG_LEVEL, MINIAPP_HOST, MINIAPP_PORT
from session_manager import SessionManager
from telegram_bot import TelegramBotController

# Windows terminals default to cp1252, which can't encode the emoji used in
# log messages. Force UTF-8 on stdio so logging never crashes mid-line.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Bot Orchestrator
# ═══════════════════════════════════════════════════════════════════════════


class AutoTraderBot:
    """Main orchestrator that ties everything together."""

    def __init__(self):
        self.telegram = TelegramBotController()

        # Multi-user Mini App sessions — every Telegram user who opens the
        # app gets their own isolated settings/SSID/trader/loop here.
        self.session_manager = SessionManager(bot=self.telegram.bot)

        self._is_running = True

    # ─────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the bot: run Telegram polling + the Mini App API."""
        logger.info("=" * 60)
        logger.info("   AUTO TRADER BOT — Starting")
        logger.info("=" * 60)
        logger.info("✅ Bot initialized! Waiting for /start command in Telegram.")
        logger.info("")

        try:
            await asyncio.gather(
                self._telegram_task(),
                self._miniapp_task(),
            )
        except asyncio.CancelledError:
            logger.info("Bot shutting down...")
        except KeyboardInterrupt:
            logger.info("Interrupted by user.")
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Clean shutdown."""
        self._is_running = False
        await self.session_manager.stop_all()
        await self.telegram.stop()
        logger.info("Bot stopped.")

    # ─────────────────────────────────────────────────────────────────────
    # Concurrent tasks
    # ─────────────────────────────────────────────────────────────────────

    async def _telegram_task(self) -> None:
        """Run Telegram bot polling (blocks until shutdown)."""
        try:
            await self.telegram.start_polling()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Telegram polling error: {e}")

    async def _miniapp_task(self) -> None:
        """Serve the Mini App REST API (multi-user). Frontend is separate."""
        try:
            import uvicorn

            from server import create_app

            app = create_app(self.session_manager)
            config = uvicorn.Config(
                app,
                host=MINIAPP_HOST,
                port=MINIAPP_PORT,
                log_level="warning",
                loop="asyncio",
            )
            server = uvicorn.Server(config)
            logger.info(f"🌐 Mini App API listening on http://{MINIAPP_HOST}:{MINIAPP_PORT}")
            await server.serve()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Mini App server error: {e}")
            logger.error(traceback.format_exc())


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    """Main entry point with auto-restart on crash."""
    max_restarts = 50
    restart_count = 0
    restart_delay = 30

    while restart_count < max_restarts:
        bot = AutoTraderBot()

        try:
            logger.info(f"🚀 Bot starting (attempt #{restart_count})...")
            await bot.start()
            break  # Normal exit

        except KeyboardInterrupt:
            logger.info("Interrupted by user.")
            await bot.stop()
            break

        except Exception as e:
            restart_count += 1
            logger.error(f"💥 FATAL ERROR: {e}")
            logger.error(traceback.format_exc())
            logger.error(
                f"Restart #{restart_count}/{max_restarts} "
                f"in {restart_delay}s..."
            )

            try:
                await bot.stop()
            except Exception:
                pass

            await asyncio.sleep(restart_delay)
            restart_delay = min(restart_delay * 1.5, 300)

    if restart_count >= max_restarts:
        logger.error(f"❌ Max restarts ({max_restarts}) reached. Exiting.")


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    print()
    print("=" * 60)
    print("   POCKET OPTION AUTO TRADER")
    print("=" * 60)
    print()

    asyncio.run(main())
