import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.database import init_database
from app.models import SourceEvent, SourceEventPrefilter
from app.prefilters import (eligible_source_events, evaluate_source_event, prefilter_source_events,
                            summarize_prefilter)


def event(title, *, source_type='atpress', source_name='@Press', event_type='press_release', summary=''):
    return SourceEvent(source_type=source_type, source_name=source_name, event_type=event_type,
        title=title, summary=summary, source_url='https://example.com/news', external_id=title)


@pytest.mark.parametrize('title', [
    '『アイドルマスター』菊地真・萩原雪歩のちびぐるみ登場',
    'クトゥルフ神話のイラスト展・記念イベント開催',
    '鳥フェス神戸2026へ「自認シマエナガ」が出展決定',
    'ZEH仕様の注文住宅施工事例',
    '「ぷにっこ」シリーズ新作登場',
])
def test_atpress_real_noise_fixtures_drop(title):
    result = evaluate_source_event(event(title), Settings())
    assert result.status == 'DROP'
    assert result.rule == 'atpress-v1'


@pytest.mark.parametrize('title', [
    '株式会社AAA、創業20周年を機にブランド・コーポレートサイトを全面刷新',
    '株式会社BBB、新規事業としてAIサービスを開始',
    '株式会社CCC、海外展開に伴い新ブランドを発表',
    '株式会社DDD、新Purpose策定およびCI刷新',
])
def test_atpress_positive_fixtures_pass(title):
    assert evaluate_source_event(event(title), Settings()).status == 'PASS'


def test_atpress_strong_positive_overrides_event_noise():
    result = evaluate_source_event(event('新ブランド立ち上げに伴い期間限定イベントを開催'), Settings())
    assert result.status == 'PASS'
    assert 'strong positive' in result.reason


@pytest.mark.parametrize(('title', 'expected_status', 'expected_type'), [
    ('KOMPEITO、東証グロース市場へ上場', 'PASS', 'ipo'),
    ('メトロウェザー株式会社に出資', 'PASS', 'investment'),
    ('ペイトナー株式会社に出資', 'PASS', 'investment'),
    ('不審な採用・入金案内への注意', 'DROP', 'warning'),
    ('アスエネ株式会社に出資', 'PASS', 'investment'),
])
def test_incubate_fund_real_fixtures(title, expected_status, expected_type):
    result = evaluate_source_event(event(title, source_type='vc_news', source_name='Incubate Fund',
                                         event_type='vc_news'), Settings())
    assert (result.status, result.classified_event_type) == (expected_status, expected_type)


def test_unknown_source_is_held_safely():
    result = evaluate_source_event(event('判断材料の少ないニュース', source_type='future_source',
                                         source_name='Future Source'), Settings())
    assert result.status == 'HOLD'
    assert result.rule == 'default-safe-v1'


def test_prefilter_persistence_metrics_and_hold_handling(tmp_path):
    factory = init_database(f'sqlite:///{tmp_path / "db"}')
    with factory.begin() as session:
        rows = [
            event('株式会社BBB、新規事業としてAIサービスを開始'),
            event('ZEH仕様の注文住宅施工事例'),
            event('判断材料の少ないニュース', source_type='future_source', source_name='Future Source'),
        ]
        session.add_all(rows)
        session.flush()
        evaluated = prefilter_source_events(session, rows, Settings())
    assert summarize_prefilter(evaluated) == {
        '@Press': {'collected': 2, 'pass': 1, 'hold': 0, 'drop': 1},
        'Future Source': {'collected': 1, 'pass': 0, 'hold': 1, 'drop': 0},
    }
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceEventPrefilter)) == 3
        dropped = session.scalar(select(SourceEvent).join(SourceEventPrefilter).where(
            SourceEventPrefilter.status == 'DROP'))
        assert dropped.processed_at is not None
        assert {item.title for item in eligible_source_events(session, 10, True)} == {
            '株式会社BBB、新規事業としてAIサービスを開始', '判断材料の少ないニュース'}
        assert [item.title for item in eligible_source_events(session, 10, False)] == [
            '株式会社BBB、新規事業としてAIサービスを開始']
    factory.kw['bind'].dispose()

