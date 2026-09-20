import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.database import init_database
from app.models import SourceEvent, SourceEventPrefilter
from app.prefilters import (eligible_source_events, evaluate_source_event, prefilter_source_events,
                            route_candidate_events, summarize_prefilter)


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
    assert result.rule == 'atpress-v2'


@pytest.mark.parametrize('title', [
    '株式会社AAA、創業20周年を機にブランド・コーポレートサイトを全面刷新',
    '株式会社BBB、新規事業としてAIサービスを開始',
    '株式会社CCC、海外展開に伴い新ブランドを発表',
    '株式会社DDD、新Purpose策定およびCI刷新',
    '株式会社EEE、リブランディングを実施',
])
def test_atpress_positive_fixtures_pass(title):
    assert evaluate_source_event(event(title), Settings()).status == 'PASS'


def test_atpress_strong_positive_overrides_event_noise():
    result = evaluate_source_event(event('新ブランド立ち上げに伴い期間限定イベントを開催'), Settings())
    assert result.status == 'PASS'
    assert 'strong_business_trigger' in result.reason


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


@pytest.mark.parametrize(('title', 'expected_type', 'expected_status'), [
    ('INDXの資金調達', 'funding', 'PASS'),
    ('SYNTHESISの資金調達', 'funding', 'PASS'),
    ('KOMPEITO、東京証券取引所グロース市場へ上場', 'ipo', 'PASS'),
    ('SQUEEZE、東京証券取引所グロース市場へ上場', 'ipo', 'PASS'),
    ('ベンチャー投資家ランキング', 'media', 'DROP'),
    ('Forbes ベンチャー投資家ランキング', 'media', 'DROP'),
    ('社名・代表者名を不正使用した文書への注意', 'warning', 'DROP'),
])
def test_vc_formal_ipo_and_investor_media_fixtures(title, expected_type, expected_status):
    result = evaluate_source_event(event(title, source_type='vc_news', source_name='Incubate Fund',
                                         event_type='vc_news'), Settings())
    assert (result.classified_event_type, result.status) == (expected_type, expected_status)


def test_vc_source_category_overrides_title_keyword():
    row = event('Forbes ベンチャー投資家ランキング', source_type='vc_news', source_name='Incubate Fund',
                event_type='investment')
    row.raw_data = {'category': 'メディア掲載'}
    result = evaluate_source_event(row, Settings())
    assert (result.classified_event_type, result.status, result.event_strength) == ('media', 'DROP', 15)


def test_vc_source_category_lists_keep_negative_category_priority():
    row = event('株式会社AAAへ出資', source_type='vc_news', source_name='Incubate Fund',
                event_type='vc_news')
    row.raw_data = {'categories': ['投資', 'メディア掲載']}
    result = evaluate_source_event(row, Settings())
    assert (result.classified_event_type, result.status) == ('media', 'DROP')


def test_vc_investor_audience_category_does_not_mean_investment():
    row = event('投資家向けセミナーを開催', source_type='vc_news', source_name='Incubate Fund',
                event_type='vc_news')
    row.raw_data = {'category': '投資家向け'}
    result = evaluate_source_event(row, Settings())
    assert (result.classified_event_type, result.status) == ('event', 'HOLD')


def test_atpress_anniversary_requires_business_trigger():
    weak = evaluate_source_event(event('a flood of circle、20周年記念アルバム'), Settings())
    strong = evaluate_source_event(event('株式会社AAA、創業20周年を機にブランドとコーポレートサイトを全面刷新'), Settings())
    assert weak.status in {'DROP', 'HOLD'} and weak.status != 'PASS'
    assert weak.event_strength <= 40
    assert (strong.status, strong.event_strength) == ('PASS', 90)


@pytest.mark.parametrize('titles', [
    ('イベント開催', 'イベントを開催'),
    ('採用サイト刷新', '採用サイトを刷新'),
    ('公式サイトリニューアル', '公式サイトをリニューアル'),
    ('新規事業開始', '新規事業を開始'),
    ('ブランド刷新', 'ブランドを刷新'),
])
def test_atpress_particle_variations_are_equivalent(titles):
    first, second = (evaluate_source_event(event(title), Settings()) for title in titles)
    assert (first.status, first.classified_event_type, first.event_strength) == (
        second.status, second.classified_event_type, second.event_strength)


def test_unknown_source_is_held_safely():
    result = evaluate_source_event(event('判断材料の少ないニュース', source_type='future_source',
                                         source_name='Future Source'), Settings())
    assert result.status == 'HOLD'
    assert result.rule == 'default-safe-v2'
    assert result.event_strength == 50


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
        '@Press': {'collected': 2, 'pass': 1, 'hold': 0, 'drop': 1, 'avg_strength': 46.0},
        'Future Source': {'collected': 1, 'pass': 0, 'hold': 1, 'drop': 0, 'avg_strength': 50.0},
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


def test_router_prefers_high_strength_vc_events_and_excludes_drops(tmp_path):
    factory = init_database(f'sqlite:///{tmp_path / "db"}')
    with factory.begin() as session:
        rows = [
            event('KOMPEITO、東京証券取引所グロース市場へ上場', source_type='vc_news', source_name='Incubate Fund', event_type='vc_news'),
            event('SQUEEZE、東京証券取引所グロース市場へ上場', source_type='vc_news', source_name='Incubate Fund', event_type='vc_news'),
            event('ベンチャー投資家ランキング', source_type='vc_news', source_name='Incubate Fund', event_type='vc_news'),
            event('a flood of circle、20周年記念アルバム'),
        ] + [event(f'イベントを開催 {index}') for index in range(30)]
        session.add_all(rows)
        session.flush()
        prefilter_source_events(session, rows, Settings())
    with factory() as session:
        selected = eligible_source_events(session, 20, True)
        titles = [row.title for row in selected]
        assert len(selected) == 20
        assert titles.index('KOMPEITO、東京証券取引所グロース市場へ上場') < 2
        assert titles.index('SQUEEZE、東京証券取引所グロース市場へ上場') < 2
        assert 'ベンチャー投資家ランキング' not in titles
        assert titles.index('a flood of circle、20周年記念アルバム') > 1
        records = list(session.execute(select(SourceEvent, SourceEventPrefilter).join(SourceEventPrefilter)))
        routed = route_candidate_events([(row, prefilter) for row, prefilter in records if prefilter.status != 'DROP'], 20)
        assert all(route.prefilter.status != 'DROP' for route in routed)
    factory.kw['bind'].dispose()


def test_router_applies_soft_source_diversity_before_repeating_a_source(tmp_path):
    factory = init_database(f'sqlite:///{tmp_path / "db"}')
    with factory.begin() as session:
        events = [
            event('source A 70', source_type='atpress', source_name='@Press'),
            event('source A 69', source_type='atpress', source_name='@Press'),
            event('source B 68', source_type='future_source', source_name='Quality Feed'),
        ]
        session.add_all(events)
        session.flush()
        for row, strength in zip(events, (70, 69, 68)):
            session.add(SourceEventPrefilter(source_event_id=row.id, status='HOLD', reason='fixture', score=0,
                rule='fixture', classified_event_type='other', event_strength=strength,
                matched_positive_signals=[], matched_negative_signals=[], supporting_signals=[]))
    with factory() as session:
        records = list(session.execute(select(SourceEvent, SourceEventPrefilter).join(SourceEventPrefilter)))
        routed = route_candidate_events(records, 3)
        assert [route.event.source_name for route in routed] == ['@Press', 'Quality Feed', '@Press']
    factory.kw['bind'].dispose()
