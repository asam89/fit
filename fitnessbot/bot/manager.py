"""ConnectionManager: start/stop per-user Telegram bot instances."""

import asyncio
import logging

from telegram.ext import Application

from fitnessbot import db
from fitnessbot.web.connections import decrypt_token
from fitnessbot.bot.handlers import register_handlers

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages per-user Telegram bot polling instances."""

    def __init__(self):
        self._bots: dict[int, Application] = {}  # user_id -> Application
        self._tasks: dict[int, asyncio.Task] = {}

    async def start_all(self) -> None:
        """Start bot instances for all active connections."""
        connections = db.get_all_active_connections()
        for conn in connections:
            await self.start_bot(conn["user_id"], conn)

    async def start_bot(self, user_id: int, conn: dict | None = None) -> bool:
        """Start a bot instance for a specific user."""
        if user_id in self._bots:
            logger.info("Bot already running for user %d", user_id)
            return True

        if conn is None:
            conn = db.get_telegram_connection(user_id)
        if not conn:
            logger.warning("No active connection for user %d", user_id)
            return False

        try:
            token = decrypt_token(conn["bot_token_encrypted"])
        except Exception as e:
            logger.error("Failed to decrypt token for user %d: %s", user_id, e)
            return False

        try:
            app = Application.builder().token(token).build()
            register_handlers(app, user_id)
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            self._bots[user_id] = app
            logger.info("Started bot for user %d (@%s)", user_id, conn.get("bot_username", "unknown"))
            return True
        except Exception as e:
            logger.error("Failed to start bot for user %d: %s", user_id, e)
            return False

    async def stop_bot(self, user_id: int) -> None:
        """Stop a bot instance for a specific user."""
        app = self._bots.pop(user_id, None)
        if app:
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
                logger.info("Stopped bot for user %d", user_id)
            except Exception as e:
                logger.error("Error stopping bot for user %d: %s", user_id, e)

    async def stop_all(self) -> None:
        """Stop all running bot instances."""
        user_ids = list(self._bots.keys())
        for uid in user_ids:
            await self.stop_bot(uid)

    async def restart_bot(self, user_id: int) -> bool:
        """Restart a bot instance (e.g. after token change)."""
        await self.stop_bot(user_id)
        return await self.start_bot(user_id)

    def is_running(self, user_id: int) -> bool:
        return user_id in self._bots

    @property
    def active_count(self) -> int:
        return len(self._bots)


# Global instance
connection_manager = ConnectionManager()
