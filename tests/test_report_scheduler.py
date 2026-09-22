import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock
from app.config import Settings
from app.report import format_error_report, split_messages, send_discord_report
from app.scheduler import daily_trigger


def test_discord_split():
    report = '日本語🙂' * 1600
    chunks = split_messages(report)
    assert ''.join(chunks) == report
    assert all(len(c.encode('utf-16-le')) // 2 <= 1900 for c in chunks)


def test_discord_split_does_not_cut_url_or_markdown_link():
    url = 'https://www.mitsue.co.jp/our_work/voice/resonabank.html'
    report = ('前段落です。\n' + ('説明 ' * 300) + f' [Resona]({url})\n後段落です。')
    chunks = split_messages(report, limit=1900)
    assert ''.join(chunks) == report
    assert any(url in chunk for chunk in chunks)
    for left, right in zip(chunks, chunks[1:]):
        assert not (left.endswith('https://www.m') and right.startswith('itsue'))


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
