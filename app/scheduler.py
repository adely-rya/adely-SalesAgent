import asyncio
import logging
import signal
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.pipeline import run_pipeline
from app.config import Settings

log = logging.getLogger(__name__)


def daily_trigger(settings: Settings) -> CronTrigger:
    return CronTrigger(hour=settings.daily_run_hour, minute=settings.daily_run_minute,
                       timezone=ZoneInfo(settings.timezone))


async def run_daemon(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # native Windows; Docker runs Linux
            pass
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))
    scheduler.add_job(run_pipeline, daily_trigger(settings), args=[settings],
                      id='daily-sales', max_instances=1, coalesce=True, misfire_grace_time=3600)
    scheduler.start()
    log.info('scheduler started daily=%02d:%02d timezone=%s',
             settings.daily_run_hour, settings.daily_run_minute, settings.timezone)
    if not settings.has_api_key:
        log.warning('OPENAI_API_KEY is not configured. Set OPENAI_API_KEY in .env. Daemon remains running.')
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
