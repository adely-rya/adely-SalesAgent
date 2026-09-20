import asyncio
import logging
import signal
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from app.collectors import collect_fixed_sources
from app.pipeline import run_pipeline
from app.config import Settings
from app.database import init_database

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


async def run_fixed_collector_daemon(settings: Settings) -> None:
    """Collect fixed sources on a configurable interval; run no AI stages."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    factory = init_database(settings.database_url)
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))
    scheduler.add_job(collect_fixed_sources, IntervalTrigger(hours=settings.fixed_collector_interval_hours),
        args=[settings, factory], id='fixed-source-collector', max_instances=1, coalesce=True,
        misfire_grace_time=3600)
    scheduler.start()
    log.info('fixed collector scheduler started interval_hours=%d', settings.fixed_collector_interval_hours)
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        factory.kw['bind'].dispose()
