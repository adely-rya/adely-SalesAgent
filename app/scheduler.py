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
from app.v2_pipeline import run_daily_pipeline

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


async def run_v2_daemon(settings: Settings) -> None:
    """Run the V2 pipeline daily and collect fixed sources on startup/every interval."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # native Windows; Docker runs Linux
            pass

    factory = init_database(settings.database_url)
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))
    job_lock = asyncio.Lock()

    async def collect_fixed_locked(trigger: str) -> None:
        try:
            result = await collect_fixed_sources(settings, factory)
            log.info('v2 fixed collection finished trigger=%s stored=%d errors=%d',
                     trigger, len(result.stored), len(result.errors))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('v2 fixed collection failed trigger=%s', trigger)

    async def collect_fixed_job() -> None:
        async with job_lock:
            await collect_fixed_locked('interval')

    async def daily_v2_job() -> None:
        async with job_lock:
            try:
                # Refresh immediately before loading Raw Items so a coincident
                # three-hour tick cannot make today's report miss fresh news.
                await collect_fixed_locked('before-daily-v2-run')
                completed = await run_daily_pipeline(settings, collect=False)
                if not completed:
                    log.warning('v2 daily pipeline finished with errors')
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('v2 daily pipeline failed before completion')

    scheduler.add_job(collect_fixed_job,
        IntervalTrigger(hours=settings.fixed_collector_interval_hours),
        id='v2-fixed-source-collector', max_instances=1, coalesce=True, misfire_grace_time=3600)
    scheduler.add_job(daily_v2_job, daily_trigger(settings), id='daily-v2-sales',
        max_instances=1, coalesce=True, misfire_grace_time=3600)
    scheduler.start()
    log.info('v2 scheduler started daily=%02d:%02d timezone=%s collector_interval_hours=%d',
             settings.daily_run_hour, settings.daily_run_minute, settings.timezone,
             settings.fixed_collector_interval_hours)
    if not settings.has_api_key:
        log.warning('OPENAI_API_KEY is not configured. The V2 daily pipeline will report this at run time.')

    try:
        # Collect once immediately instead of waiting for the first interval tick.
        async with job_lock:
            await collect_fixed_locked('startup')
        log.info('v2 startup fixed collection completed')
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        factory.kw['bind'].dispose()


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
