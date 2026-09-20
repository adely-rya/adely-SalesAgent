import asyncio
import json

from sqlalchemy import func, select

from app.collectors import (CollectionResult, SourceDefinition, collect_fixed_sources,
                            parse_incubate_fund_news)
from app.config import Settings
from app.database import init_database
from app.domain import RawItem
from app.gating import gate_status, hard_filter
from app.llm import Generation
from app.models import CandidateRecord, Diagnostic, ResearchScore, SourceEvent, VCProfile
from app.schemas import (CheapWinOutput, CurrentExpression, DiagnosticOutput, EvaluationOutput, Event,
                         EventEvidence, ExpressionDebt, FixedEventItem, FixedEventOutput,
                         CreativeLockIn, NeedScores, PeerGap, StageEvidence, StrategyOutput, VCProfileInput,
                         WinScores, DeliverScores, EvidenceCoverage)
from app.v2_discovery import extract_fixed_events, group_events_by_company, merge_events
from app.v2_pipeline import run_daily_pipeline, run_v2_pipeline
from app.vc_profiles import import_vc_profiles, profile_context


def evaluation_output():
    return EvaluationOutput(
        need=NeedScores(**dict.fromkeys(NeedScores.model_fields, 8)),
        win=WinScores(**dict.fromkeys(WinScores.model_fields, 7)),
        deliver=DeliverScores(**dict.fromkeys(DeliverScores.model_fields, 6)),
        scope_hypothesis='事業の変化を伝える60秒のブランド映像という仮説',
        evidence_coverage=EvidenceCoverage(**{
            name: StageEvidence(coverage='medium', reason='公開情報を確認')
            for name in ('need', 'win', 'deliver')
        }),
        reason='変化と表現課題があり、制作規模は要確認', strongest_signals=['新規事業'],
        risks=['予算は未確認'], research_needed=['決裁経路'],
    )


def diagnostic_output():
    return DiagnosticOutput(
        current_expression=CurrentExpression(assets=[], summary='既存サイトを確認', unknowns=['動画の網羅確認は未実施']),
        expression_debt=ExpressionDebt(score=7, business_change='新規事業開始', expression_gap='説明表現は限定的',
                                        reason='事業変化に対する説明の追加余地', confidence='medium'),
        peer_gap=None,
        creative_lock_in=CreativeLockIn(status='unknown', partners_or_credits=[], observation='明示的な制作クレジットは未確認',
                                        evidence_confidence='low'),
    )


def fixed_candidate(source_url: str):
    from app.schemas import Candidate
    return Candidate(company_name='株式会社ABC', website='https://abc.example', trigger_type='new_business',
        trigger_title='新規事業を開始', trigger_summary='技術サービスの新規事業を開始した',
        published_at='2026-09-10', source_url=source_url, source_title='公式発表', location='東京都',
        possible_video_need='事業の価値を説明するブランド映像', research_facts=[], research_unknowns=['既存表現'])


def event_fixture(*, title='新規事業を開始', company_name='株式会社ABC', source_type='web_search',
                  source_name='Web Search', source_url='https://example.com/news/1', event_type='new_business',
                  strength=80, website='https://abc.example'):
    evidence = EventEvidence(source_type=source_type, source_name=source_name,
        source_url=source_url, source_title=title, raw_item_id=1 if source_type != 'web_search' else None)
    return Event(company_name=company_name, event_type=event_type, title=title,
        summary='サービス事業を開始し、新しい顧客へ事業内容を説明する', published_at='2026-09-10',
        source_type=source_type, source_name=source_name, source_url=source_url,
        source_title=title, evidence=[evidence], strength=strength,
        company_website=website, location='東京都', possible_video_need='事業価値を説明する映像')


def test_rss_collection_persists_source_events_once(tmp_path):
    source = SourceDefinition('atpress', '@Press', 'https://unit.example/feed.xml', 'rss')
    pages = {
        'https://unit.example/robots.txt': (200, 'User-agent: *\nAllow: /'),
        source.url: (200, '''<rss><channel><item><title>新サービス開始</title><link>/news/1</link>
            <guid>news-1</guid><description>株式会社ABCの発表</description><pubDate>Wed, 10 Sep 2026 10:00:00 +0900</pubDate>
            </item></channel></rss>'''),
    }

    async def fetch(url):
        return pages[url]

    factory = init_database(f'sqlite:///{tmp_path / "db"}')
    first = asyncio.run(collect_fixed_sources(Settings(), factory, sources=(source,), fetcher=fetch))
    second = asyncio.run(collect_fixed_sources(Settings(), factory, sources=(source,), fetcher=fetch))
    with factory() as session:
        event = session.scalar(select(SourceEvent))
        assert session.scalar(select(func.count()).select_from(SourceEvent)) == 1
        assert event.source_url == 'https://unit.example/news/1'
        assert event.event_type == 'press_release'
    assert len(first.stored) == len(second.stored) == 1
    factory.kw['bind'].dispose()


def test_vc_static_listing_parser_is_bounded_to_articles():
    events = parse_incubate_fund_news('''<div class="parts-news-item-wrapper"><div><date>2026.09.10</date>
        <div>新規・追加投資</div></div><div><a href="/news/investment-1"><h3>株式会社ABCに出資しました</h3></a>
        <p>公式発表です</p></div></div>''',
        'https://incubatefund.com/news/')
    assert len(events) == 1
    assert events[0].event_type == 'vc_news'
    assert events[0].raw_data == {'category': '新規・追加投資'}
    assert events[0].published_at.year == 2026


def test_merge_and_group_preserve_events_and_independent_evidence():
    fixed = event_fixture(source_type='vc_news', source_name='Incubate Fund',
        source_url='https://vc.example/news/1', website=None)
    web = event_fixture(source_type='web_search', source_name='Web Search',
        source_url='https://news.example/story/1', website='https://abc.example/about')
    merged = merge_events([fixed], [web])
    opportunities = group_events_by_company(merged)
    assert len(merged) == 1
    assert len(opportunities) == 1
    assert opportunities[0].origins == ['vc_news', 'web_search']
    assert len(merged[0].evidence) == 2
    assert str(opportunities[0].candidate.website) == 'https://abc.example/about'
    assert len(opportunities[0].events) == 1


def test_hard_filter_and_gate_are_conservative():
    opportunity = group_events_by_company([event_fixture()])[0]
    assert not hard_filter(opportunity).excluded
    filtered = hard_filter(opportunity)
    assert '構造化された確認事実' in filtered.reason
    settings = Settings(win_pre_drop_threshold=3.5, win_pre_diagnostic_threshold=5.5)
    assert gate_status(CheapWinOutput(win_pre=2, confidence='low', hard_blocker=False, risk_tags=[], reason='弱い'), settings) == 'drop'
    assert gate_status(CheapWinOutput(win_pre=4, confidence='medium', hard_blocker=False, risk_tags=[], reason='保留'), settings) == 'hold'
    assert gate_status(CheapWinOutput(win_pre=6, confidence='medium', hard_blocker=False, risk_tags=[], reason='進める'), settings) == 'research'


def test_adversarial_event_words_never_establish_company_business_type():
    for trigger_text in ('映像制作会社向けSaaSを開始', '広告代理店と協業',
                         '東証プライム企業との共同実証', 'ブランド刷新に伴い映像制作を強化'):
        opportunity = group_events_by_company([event_fixture(title=trigger_text)])[0]
        assert not hard_filter(opportunity).excluded


def test_vc_profile_import_and_lookup(tmp_path):
    factory = init_database(f'sqlite:///{tmp_path / "db"}')
    with factory.begin() as session:
        assert import_vc_profiles(session, [VCProfileInput(name='Incubate Fund', stage_focus=['seed'],
            creative_support=True, creative_support_level=3, evidence=['https://vc.example/support'])]) == 1
    with factory() as session:
        context = profile_context(session, {'Incubate Fund'})
        assert context[0]['creative_support_level'] == 3
        assert session.scalar(select(VCProfile)).name == 'Incubate Fund'
    factory.kw['bind'].dispose()


class FixedClient:
    def __init__(self):
        self.calls = []

    def prompt(self, name):
        return name

    async def generate(self, *, output_type, input_text, **kwargs):
        self.calls.append((output_type, kwargs))
        raw_item = json.loads(input_text)['raw_items'][0]
        event = Event(company_name='株式会社ABC', event_type='investment', title=raw_item['title'],
            summary='株式会社ABCに出資', source_type=raw_item['source_type'],
            source_name=raw_item['source_name'], source_url=raw_item['source_url'],
            source_title=raw_item['title'],
            evidence=[EventEvidence(source_type=raw_item['source_type'], source_name=raw_item['source_name'],
                source_url=raw_item['source_url'], source_title=raw_item['title'], raw_item_id=raw_item['id'])])
        return Generation(FixedEventOutput(events=[FixedEventItem(event=event,
            raw_item_ids=[raw_item['id']])]), set())


class CaptureFixedBatchClient:
    def __init__(self):
        self.input_text = None

    def prompt(self, _name):
        return 'fixed extraction fixture'

    async def generate(self, *, input_text, output_type, **kwargs):
        self.input_text = input_text
        assert output_type is FixedEventOutput
        assert kwargs['use_web_search'] is False
        return Generation(FixedEventOutput(events=[]), set())


def test_fixed_event_extraction_never_enables_web_search():
    raw_item = RawItem(id=1, source_type='vc_news', source_name='Incubate Fund',
        event_type='investment', company_name='株式会社ABC', title='ABCへ出資', summary='',
        published_at=None, source_url='https://vc.example/news/1', raw_data={'category': '投資'})
    client = FixedClient()
    events, _decisions, processed_ids, rejected = asyncio.run(
        extract_fixed_events(client, Settings(), [raw_item]))
    assert len(events) == 1 and events[0].source_type == 'vc_news'
    assert events[0].strength == 100
    assert processed_ids == {1} and rejected == 0
    assert client.calls[0][1]['use_web_search'] is False


def test_fixed_event_router_keeps_ipo_ahead_of_weak_items_and_excludes_drops():
    raw_items = [
        RawItem(id=1, source_type='vc_news', source_name='Incubate Fund', event_type='vc_news',
            company_name='KOMPEITO', title='KOMPEITO、東京証券取引所グロース市場へ上場',
            summary='', published_at=None, source_url='https://vc.example/ipo/kompeito', raw_data={}),
        RawItem(id=2, source_type='vc_news', source_name='Incubate Fund', event_type='vc_news',
            company_name='SQUEEZE', title='SQUEEZE、東京証券取引所グロース市場へ上場',
            summary='', published_at=None, source_url='https://vc.example/ipo/squeeze', raw_data={}),
        RawItem(id=3, source_type='vc_news', source_name='Incubate Fund', event_type='vc_news',
            company_name=None, title='Forbes ベンチャー投資家ランキング', summary='',
            published_at=None, source_url='https://vc.example/media/ranking', raw_data={}),
        RawItem(id=4, source_type='atpress', source_name='@Press', event_type='press_release',
            company_name=None, title='a flood of circle、20周年記念アルバム', summary='',
            published_at=None, source_url='https://press.example/anniversary', raw_data={}),
    ]
    raw_items.extend(RawItem(id=index, source_type='atpress', source_name='@Press',
        event_type='press_release', company_name=None, title=f'単発イベントを開催 {index}', summary='',
        published_at=None, source_url=f'https://press.example/events/{index}', raw_data={})
        for index in range(5, 26))
    client = CaptureFixedBatchClient()
    settings = Settings(fixed_discovery_batch_size=20)
    events, decisions, consumed, rejected = asyncio.run(extract_fixed_events(client, settings, raw_items))
    routed_titles = [item['title'] for item in json.loads(client.input_text)['raw_items']]
    assert len(routed_titles) == 20
    assert set(routed_titles[:2]) == {raw_items[0].title, raw_items[1].title}
    assert raw_items[2].title not in routed_titles
    assert decisions[3].status == 'DROP'
    assert decisions[4].status in {'DROP', 'HOLD'}
    assert {item['prefilter_status'] for item in json.loads(client.input_text)['raw_items']} <= {'PASS', 'HOLD'}
    assert len(consumed) == 21 and rejected == 0 and events == []


class V2MockClient:
    """All model stages are pure fixtures; this client cannot make a network request."""
    def __init__(self):
        self.calls = []

    def prompt(self, name):
        return name

    async def generate(self, *, output_type, input_text, **kwargs):
        self.calls.append((output_type, kwargs, input_text))
        if output_type is FixedEventOutput:
            raw_item = json.loads(input_text)['raw_items'][0]
            event = Event(company_name='株式会社ABC', event_type='investment', title=raw_item['title'],
                summary='技術サービスへの出資', published_at=raw_item['published_at'],
                source_type=raw_item['source_type'], source_name=raw_item['source_name'],
                source_url=raw_item['source_url'], source_title=raw_item['title'],
                evidence=[EventEvidence(source_type=raw_item['source_type'], source_name=raw_item['source_name'],
                    source_url=raw_item['source_url'], source_title=raw_item['title'], raw_item_id=raw_item['id'])],
                strength=raw_item['event_strength'], company_website='https://abc.example', location='東京都',
                possible_video_need='技術サービスのブランド映像')
            return Generation(FixedEventOutput(events=[FixedEventItem(event=event,
                raw_item_ids=[raw_item['id']])]), set())
        if output_type is CheapWinOutput:
            return Generation(CheapWinOutput(win_pre=6.5, confidence='medium', hard_blocker=False,
                              risk_tags=[], reason='小規模の提案余地がある'), set())
        if output_type is DiagnosticOutput:
            return Generation(diagnostic_output(), {'https://abc.example'})
        if output_type is EvaluationOutput:
            return Generation(evaluation_output(), set())
        raise AssertionError(output_type)


def test_v2_pipeline_runs_only_mocked_expensive_stages(tmp_path):
    async def fake_collector(_settings, factory):
        with factory.begin() as session:
            session.add_all([
                SourceEvent(source_type='vc_news', source_name='Incubate Fund', event_type='investment',
                    title='株式会社ABCへ出資', summary='技術サービス',
                    source_url='https://vc.example/news/1', external_id='event-1'),
                SourceEvent(source_type='vc_news', source_name='Incubate Fund', event_type='vc_news',
                    title='不審な採用・入金案内への注意', summary='',
                    source_url='https://vc.example/news/warning', external_id='event-warning'),
            ])
        return CollectionResult([], [])

    settings = Settings(openai_api_key='fake-test', database_url=f'sqlite:///{tmp_path / "db"}',
                        diagnostic_web_search=True, peer_research_enabled=False)
    client = V2MockClient()
    assert asyncio.run(run_v2_pipeline(settings, client, web_discovery=False, collector=fake_collector))
    factory = init_database(settings.database_url)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceEvent)) == 2
        assert all(item.processed_at is not None for item in session.scalars(select(SourceEvent)))
        assert session.scalar(select(CandidateRecord)).status == 'scored'
        assert session.scalar(select(func.count()).select_from(Diagnostic)) == 1
        assert session.scalar(select(func.count()).select_from(ResearchScore)) == 1
        assert session.scalar(select(ResearchScore)).selected_rank == 1
    fixed_input = next(json.loads(input_text) for output, _kwargs, input_text in client.calls
                       if output is FixedEventOutput)
    assert [item['title'] for item in fixed_input['raw_items']] == ['株式会社ABCへ出資']
    diagnostic_call = next(kwargs for output, kwargs, _input in client.calls if output is DiagnosticOutput)
    assert diagnostic_call['use_web_search'] is True
    factory.kw['bind'].dispose()


def test_daily_pipeline_does_not_collect_fixed_sources_by_default(tmp_path):
    calls = []

    async def unexpected_collection(*_args):
        calls.append('collect')
        raise AssertionError('daily-run must use the independently scheduled Raw Item Store')

    settings = Settings(openai_api_key='fake-test', database_url=f'sqlite:///{tmp_path / "db"}')
    assert asyncio.run(run_daily_pipeline(settings, V2MockClient(), web_discovery=False,
        fixed_discovery=False, collector=unexpected_collection))
    assert calls == []
