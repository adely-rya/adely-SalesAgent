import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock
from app.config import Settings
from app.report import format_error_report, split_messages, send_discord_report
from app.scheduler import daily_trigger, run_v2_daemon


def test_discord_split():
    report = '日本語🙂' * 1600
    chunks = split_messages(report)
    assert ''.join(chunks) == report
    assert all(len(c.encode('utf-16-le')) // 2 <= 1900 for c in chunks)


def test_discord_dummy_no_network(monkeypatch, caplog):
    network = AsyncMock()
    monkeypatch.setattr('httpx.AsyncClient.post', network)
    with caplog.at_level('INFO'):
        asyncio.run(send_discord_report('Report', 'hogehoge'))
    network.assert_not_called()
    assert 'Report' in caplog.text


def test_error_report_contains_safe_summary():
    report = format_error_report(42, 'failed', ['pipeline: ValueError'])
    assert 'Run: 42' in report
    assert 'pipeline: ValueError' in report


def test_schedule():
    now = datetime(2026, 9, 12, 7, 0, tzinfo=ZoneInfo('Asia/Tokyo'))
    assert daily_trigger(Settings()).get_next_fire_time(None, now).hour == 8


def test_v2_daemon_collects_on_startup_and_schedules_collection_and_report(monkeypatch):
    import app.scheduler as scheduler_module

    calls = []
    factory = type('Factory', (), {'kw': {'bind': type('Bind', (), {'dispose': lambda self: None})()}})()
    scheduled = {}
    started = []

    class FakeScheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, callback, trigger, **kwargs):
            scheduled[kwargs['id']] = (callback, trigger, kwargs)

        def start(self):
            started.append(True)

        def shutdown(self, wait=False):
            assert wait is False

    async def fake_collect(_settings, _factory):
        calls.append('collect')
        return type('Collection', (), {'stored': [], 'errors': []})()

    async def fake_daily(_settings, *, collect=False):
        calls.append(('daily-v2', collect))
        return True

    monkeypatch.setattr(scheduler_module, 'init_database', lambda _url: factory)
    monkeypatch.setattr(scheduler_module, 'collect_fixed_sources', fake_collect)
    monkeypatch.setattr(scheduler_module, 'run_daily_pipeline', fake_daily)
    monkeypatch.setattr(scheduler_module, 'AsyncIOScheduler', FakeScheduler)

    async def exercise():
        task = asyncio.create_task(run_v2_daemon(Settings(fixed_collector_interval_hours=3)))
        while not started:
            await asyncio.sleep(0)
        await scheduled['v2-fixed-source-collector'][0]()
        await scheduled['daily-v2-sales'][0]()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())
    assert calls == ['collect', 'collect', 'collect', ('daily-v2', False)]
    interval = scheduled['v2-fixed-source-collector'][1]
    assert interval.interval.total_seconds() == 3 * 60 * 60
    cron = scheduled['daily-v2-sales'][1]
    assert cron.timezone.key == 'Asia/Tokyo'
    assert scheduled['daily-v2-sales'][2]['max_instances'] == 1
