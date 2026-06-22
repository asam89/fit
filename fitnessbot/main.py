"""Main entry point — wires FastAPI dashboard + Telegram bot manager."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from fitnessbot.config import Config
from fitnessbot import db
from fitnessbot.bot.manager import connection_manager
from fitnessbot.scheduler import start_scheduler, shutdown_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Startup
    db.init_db()
    db.run_migrations()

    async def start_bots():
        await asyncio.sleep(2)
        logger.info("Starting Telegram bot connections...")
        await connection_manager.start_all()
        logger.info("Active bots: %d", connection_manager.active_count)

    asyncio.create_task(start_bots())
    start_scheduler()
    yield
    # Shutdown
    shutdown_scheduler()
    logger.info("Shutting down bot connections...")
    await connection_manager.stop_all()


def create_app_with_lifespan() -> FastAPI:
    from fitnessbot.web.app import create_app
    application = create_app()
    application.router.lifespan_context = lifespan
    return application


app = create_app_with_lifespan()


def main():
    db.init_db()
    db.run_migrations()
    logger.info(
        "Starting Fitness & Health Intelligence Platform on %s:%s",
        Config.DASHBOARD_HOST,
        Config.DASHBOARD_PORT,
    )
    uvicorn.run(
        "fitnessbot.main:app",
        host=Config.DASHBOARD_HOST,
        port=Config.DASHBOARD_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
