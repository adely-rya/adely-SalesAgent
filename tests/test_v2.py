import asyncio
import json

from sqlalchemy import func, select

from app.collectors import (CollectionResult, SourceDefinition, collect_fixed_sources,
                            parse_incubate_fund_news)
from app.config import Settings
from app.database import init_database
from app.gating import gate_status, hard_filter
from app.llm import Generation
from app.models import CandidateRecord, Diagnostic, ResearchScore, SourceEvent, VCProfile
from app.schemas import (CheapWinOutput, CurrentExpression, DiagnosticOutput, EvaluationOutput,
                         ExpressionDebt, FixedDiscoveryItem, FixedDiscoveryOutput, CreativeLockIn,
                         NeedScores, PeerGap, StageEvidence, StrategyOutput, VCProfileInput,
                         WinScores, DeliverScores, EvidenceCoverage)
from app.v2_discovery import CandidateEnvelope, CandidateSource, discover_fixed_events, merge_candidates
from app.v2_pipeline import run_v2_pipeline
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
    events = parse_incubate_fund_news('''<article><time>2026.09.10</time><span>新規・追加投資</span>
        <a href="/news/investment-1">株式会社ABCに出資しました</a><p>公式発表です</p></article>''',
        'https://incubatefund.com/news/')
    assert len(events) == 1
    assert events[0].event_type == 'investment'
    assert events[0].published_at.year == 2026


def test_merge_preserves_web_and_vc_origins(candidate):
    fixed = fixed_candidate('https://vc.example/news/1').model_copy(update={'website': None})
    web = candidate.model_copy(update={'company_name': 'ABC 株式会社', 'website': 'https://abc.example/about',
                                       'trigger_title': '採用を拡大', 'trigger_type': 'hiring'})
    merged = merge_candidates([
        CandidateEnvelope(fixed, (CandidateSource('vc_news', 'Incubate Fund', str(fixed.source_url), 1),)),
        CandidateEnvelope(web, (CandidateSource('web_search', 'OpenAI Web Search', str(web.source_url)),)),
    ])
    assert len(merged) == 1
    assert merged[0].origins == ['vc_news', 'web_search']
    assert len(merged[0].triggers) == 2
    assert str(merged[0].primary.website) == 'https://abc.example/about'


def test_hard_filter_and_gate_are_conservative(candidate):
    opportunity = merge_candidates([CandidateEnvelope(candidate, (
        CandidateSource('web_search', 'OpenAI Web Search', str(candidate.source_url)),))])[0]
    assert not hard_filter(opportunity).excluded
    agency = opportunity.primary.model_copy(update={'trigger_summary': '映像制作会社として新サービスを開始'})
    filtered = hard_filter(merge_candidates([CandidateEnvelope(agency, opportunity.sources)])[0])
    assert filtered.excluded and 'direct_competitor_or_agency' in filtered.risk_tags
    settings = Settings(win_pre_drop_threshold=3.5, win_pre_diagnostic_threshold=5.5)
    assert gate_status(CheapWinOutput(win_pre=2, confidence='low', hard_blocker=False, risk_tags=[], reason='弱い'), settings) == 'dropped'
    assert gate_status(CheapWinOutput(win_pre=4, confidence='medium', hard_blocker=False, risk_tags=[], reason='保留'), settings) == 'hold'
    assert gate_status(CheapWinOutput(win_pre=6, confidence='medium', hard_blocker=False, risk_tags=[], reason='進める'), settings) == 'diagnostic'


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
        event_id = json.loads(input_text)['source_events'][0]['id']
        return Generation(FixedDiscoveryOutput(candidates=[FixedDiscoveryItem(
            candidate=fixed_candidate('https://vc.example/news/1'), source_event_ids=[event_id])]), set())


def test_fixed_discovery_never_enables_web_search(tmp_path):
    factory = init_database(f'sqlite:///{tmp_path / "db"}')
    with factory.begin() as session:
        event = SourceEvent(source_type='vc_news', source_name='Incubate Fund', event_type='investment',
            title='ABCへ出資', summary='', source_url='https://vc.example/news/1', external_id='1')
        session.add(event)
        session.flush()
        event_id = event.id
    with factory() as session:
        event = session.get(SourceEvent, event_id)
        client = FixedClient()
        result = asyncio.run(discover_fixed_events(client, Settings(), [event]))
    assert result.candidates[0].origins == ['vc_news']
    assert client.calls[0][1]['use_web_search'] is False
    factory.kw['bind'].dispose()


class V2MockClient:
    """All model stages are pure fixtures; this client cannot make a network request."""
    def __init__(self):
        self.calls = []

    def prompt(self, name):
        return name

    async def generate(self, *, output_type, input_text, **kwargs):
        self.calls.append((output_type, kwargs))
        if output_type is FixedDiscoveryOutput:
            event = json.loads(input_text)['source_events'][0]
            return Generation(FixedDiscoveryOutput(candidates=[FixedDiscoveryItem(
                candidate=fixed_candidate(event['source_url']), source_event_ids=[event['id']])]), set())
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
            session.add(SourceEvent(source_type='vc_news', source_name='Incubate Fund', event_type='investment',
                title='株式会社ABCへ出資', summary='技術サービス', source_url='https://vc.example/news/1', external_id='event-1'))
        return CollectionResult([], [])

    settings = Settings(openai_api_key='fake-test', database_url=f'sqlite:///{tmp_path / "db"}',
                        diagnostic_web_search=True, peer_research_enabled=False)
    client = V2MockClient()
    assert asyncio.run(run_v2_pipeline(settings, client, web_discovery=False, collector=fake_collector))
    factory = init_database(settings.database_url)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceEvent)) == 1
        assert session.scalar(select(SourceEvent)).processed_at is not None
        assert session.scalar(select(CandidateRecord)).status == 'scored'
        assert session.scalar(select(func.count()).select_from(Diagnostic)) == 1
        assert session.scalar(select(func.count()).select_from(ResearchScore)) == 1
        assert session.scalar(select(ResearchScore)).selected_rank == 1
    diagnostic_call = next(kwargs for output, kwargs in client.calls if output is DiagnosticOutput)
    assert diagnostic_call['use_web_search'] is True
    factory.kw['bind'].dispose()
