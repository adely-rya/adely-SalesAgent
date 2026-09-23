import asyncio
import json
from collections import Counter
from datetime import date, timedelta
import pytest
import httpx
from openai import APITimeoutError

from sqlalchemy import func, select

from app.collectors import (CollectionResult, SourceDefinition, collect_fixed_sources,
                            parse_incubate_fund_news)
from app.config import Settings
from app.database import init_database
from app.domain import Opportunity, RawItem
from app.diagnostics import (DiagnosticFailure, classify_diagnostic_failure,
                             run_diagnostic_research, safe_diagnostic_url)
from app.gating import (classify_event_semantics, gate_decision, gate_status,
                        hard_filter, researchable_event_signals)
from app.llm import Generation, InvalidOutputError
from app.models import CandidateRecord, Diagnostic, ProcessingError, ResearchScore, Run, SourceEvent, VCProfile
from app.schemas import (CheapWinBatchOutput, CheapWinBatchResult, CheapWinOutput, CurrentExpression, DiagnosticOutput, EvaluationOutput, Event,
                         EventEvidence, EventInterpretation, ExpressionDebt, FixedEventItem, FixedEventOutput,
                         CreativeLockIn, NeedScores, PeerGap, StageEvidence, StrategyOutput, VCProfileInput,
                         WinScores, DeliverScores, EvidenceCoverage, EventDiscoveryOutput,
                         ResearchEvidence, ResearchFact, RiskAssessment, WebEventInterpretation)
from app.v2_discovery import (discover_web_events, extract_fixed_events, group_events_by_company,
                              merge_events, raw_item_payload)
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
        risks=[], unknowns=['予算は未確認'], research_needed=['決裁経路'],
    )


def diagnostic_output():
    return DiagnosticOutput(
        current_expression=CurrentExpression(assets=[], summary='既存サイトを確認', unknowns=['動画の網羅確認は未実施']),
        expression_debt=ExpressionDebt(score=7, business_change='新規事業開始', expression_gap='説明表現は限定的',
                                        reason='事業変化に対する説明の追加余地', confidence='medium'),
        peer_gap=None,
        creative_lock_in=CreativeLockIn(status='unknown', partners_or_credits=[], observation='明示的な制作クレジットは未確認',
                                        evidence_confidence='low'),
        evidence=[ResearchEvidence(claim='Discoveryで確認した企業Event', evidence_type='observed',
                                   source_url='https://abc.example', confidence='medium')],
    )


def fixed_candidate(source_url: str):
    from app.schemas import Candidate
    return Candidate(company_name='株式会社ABC', website='https://abc.example', trigger_type='new_business',
        trigger_title='新規事業を開始', trigger_summary='技術サービスの新規事業を開始した',
        published_at='2026-09-10', source_url=source_url, source_title='公式発表', location='東京都',
        possible_video_need='事業の価値を説明するブランド映像', research_facts=[], research_unknowns=['既存表現'])


def event_fixture(*, title='新規事業を開始', company_name='株式会社ABC', source_type='web_search',
                  source_name='Web Search', source_url='https://example.com/news/1', event_type='new_business',
                  strength=80, website='https://abc.example',
                  possible_video_need='事業価値を説明する映像'):
    evidence = EventEvidence(source_type=source_type, source_name=source_name,
        source_url=source_url, source_title=title, raw_item_id=1 if source_type != 'web_search' else None)
    return Event(company_name=company_name, event_type=event_type, title=title,
        summary='企業の発表内容を確認するための参照本文', published_at='2026-09-10',
        source_type=source_type, source_name=source_name, source_url=source_url,
        source_title=title, evidence=[evidence], strength=strength,
        company_website=website, location='東京都', possible_video_need=possible_video_need)


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
    weak_opportunity = group_events_by_company([event_fixture(event_type='store_renovation',
        title='店舗改装', strength=50)])[0]
    assert gate_status(CheapWinOutput(win_pre=2, confidence='low', hard_blocker=False, risk_tags=[], reason='弱い'),
                       settings, weak_opportunity) == 'hold'
    assert gate_status(CheapWinOutput(win_pre=8, confidence='low', hard_blocker=False, risk_tags=[],
                                      unknown_factors=['購買経路未確認'], reason='高得点だが情報不足'),
                       settings, weak_opportunity) == 'hold'
    assert gate_status(CheapWinOutput(win_pre=2, confidence='medium', hard_blocker=False,
                                      risk_tags=['調達不可が明記'], reason='逆風を確認'),
                       settings, weak_opportunity) == 'hold'
    assert gate_status(CheapWinOutput(win_pre=2, confidence='high', hard_blocker=False,
                                      risk_tags=['調達不可が明記'], reason='直接的な逆風を複数確認'),
                       settings, weak_opportunity) == 'drop'
    assert gate_status(CheapWinOutput(win_pre=4, confidence='medium', hard_blocker=False, risk_tags=[], reason='保留'),
                       settings, weak_opportunity) == 'hold'
    assert gate_status(CheapWinOutput(win_pre=6, confidence='medium', hard_blocker=False, risk_tags=[], reason='進める'),
                       settings, weak_opportunity) == 'research'
    assert gate_status(CheapWinOutput(win_pre=9, confidence='low', hard_blocker=True, risk_tags=[],
                                      reason='確定Hard blocker'), settings, weak_opportunity) == 'drop'


def test_gate_researches_strong_event_despite_low_win_confidence_and_researchable_unknowns():
    settings = Settings()
    kompeito = group_events_by_company([event_fixture(company_name='KOMPEITO', title='東京証券取引所グロース市場へ上場',
        event_type='ipo', strength=100)])[0]
    result = CheapWinOutput(win_pre=4, confidence='low', hard_blocker=False, risk_tags=[],
        unknown_factors=['購買経路未確認', 'Creative Partner未確認'], reason='現在の購買可能性は不明')
    status, reason, signals = gate_decision(result, settings, kompeito)
    assert status == 'research'
    assert 'Deep Research' in reason
    assert signals
    high_win_but_uncertain = result.model_copy(update={'win_pre': 8})
    assert gate_status(high_win_but_uncertain, settings, kompeito) == 'research'


def test_gate_keeps_startup_growth_signals_separate_from_current_win():
    settings = Settings()
    startup_events = [
        event_fixture(company_name='Seed Startup', event_type='funding', title='Seed調達', strength=50),
        event_fixture(company_name='Seed Startup', event_type='new_service_launch', title='新サービス開始',
                      source_url='https://example.com/news/2', strength=50),
        event_fixture(company_name='Seed Startup', event_type='hiring_expansion', title='採用拡大',
                      source_url='https://example.com/news/3', strength=50),
    ]
    startup = group_events_by_company(startup_events)[0]
    result = CheapWinOutput(win_pre=3, confidence='low', hard_blocker=False, risk_tags=[],
        unknown_factors=['現在予算不明'], reason='小規模だが成長Signalあり')
    assert gate_status(result, settings, startup) == 'research'
    assert result.win_pre == 3  # Growth signals allocate research; they do not inflate current WIN.


def test_gate_researches_large_company_and_brand_or_site_change_without_win_confidence():
    settings = Settings()
    result = CheapWinOutput(win_pre=4, confidence='low', hard_blocker=False, risk_tags=[],
        unknown_factors=['購買経路不明'], reason='現在の購買情報は未確認')
    for company, event_type in (
        ('株式会社ABC', 'コーポレート・アイデンティティ刷新'),
        ('株式会社DEF', 'corporate_site_renewal'),
        ('株式会社GHI', '企業文化変革'),
        ('株式会社JKL', '周年・企業スローガン刷新'),
    ):
        opportunity = group_events_by_company([event_fixture(company_name=company,
            event_type=event_type, strength=50)])[0]
        assert gate_status(result, settings, opportunity) == 'research'
    nec_service = group_events_by_company([event_fixture(company_name='NEC',
        event_type='new_service_launch', title='NECが自律型AIセキュリティサービスを開始',
        strength=50)])[0]
    assert gate_status(result, settings, nec_service) == 'research'


def test_listed_parent_is_held_but_model_can_leave_other_company_types_eligible():
    settings = Settings()
    opportunity = group_events_by_company([event_fixture(company_name='NEC',
        event_type='new_service_launch', title='NECが新サービスを開始', strength=80)])[0]
    listed = CheapWinOutput(win_pre=8, confidence='high', hard_blocker=False,
        listed_company=True, risk_tags=[], reason='対象企業本体が上場企業')
    status, reason, signals = gate_decision(listed, settings, opportunity)
    assert status == 'hold'
    assert '上場企業本体' in reason
    assert signals == ['listed_company_parent']

    subsidiary = listed.model_copy(update={'listed_company': False})
    assert gate_status(subsidiary, settings, opportunity) == 'research'


def test_gate_holds_weak_static_business_but_does_not_drop_for_size_or_unknowns():
    settings = Settings()
    static = group_events_by_company([event_fixture(company_name='Small Local Store',
        event_type='store_renovation', title='店舗改装', strength=50)])[0]
    result = CheapWinOutput(win_pre=2.5, confidence='low', hard_blocker=False, risk_tags=[],
        unknown_factors=['購買経路不明'], reason='Eventが軽微でGrowth Signalなし')
    assert gate_status(result, settings, static) == 'hold'
    assert gate_status(CheapWinOutput(win_pre=1, confidence='high', hard_blocker=False, risk_tags=[],
        reason='低Scoreだが確認済み逆風なし'), settings, static) == 'hold'


def test_gate_distinguishes_confirmed_risk_from_unknown_and_inference():
    result = CheapWinOutput(win_pre=4, confidence='low', hard_blocker=False,
        risk_tags=['入札案件のみと公式に確認'], unknown_factors=['購買担当者不明'],
        inference_factors=['大企業のため承認経路が複雑な可能性'], reason='Fact/Risk/Unknown/Inferenceを区別')
    assert result.risk_tags == ['入札案件のみと公式に確認']
    assert result.unknown_factors == ['購買担当者不明']
    assert result.inference_factors == ['大企業のため承認経路が複雑な可能性']


def test_gate_fundamental_event_gap_holds_and_low_confidence_alone_does_not():
    settings = Settings()
    result = CheapWinOutput(win_pre=5, confidence='low', hard_blocker=False, risk_tags=[],
        unknown_factors=['予算不明'], reason='購買情報は不明')
    malformed = Opportunity(company_name='株式会社ABC', events=[], candidate=fixed_candidate('https://abc.example'))
    assert gate_status(result, settings, malformed) == 'hold'
    strong_event = group_events_by_company([event_fixture(event_type='ipo', title='東京証券取引所へ上場', strength=100,
        possible_video_need='上場後の事業内容を企業顧客と採用候補者へ説明する映像需要は未確認')])[0]
    assert gate_status(result, settings, strong_event) == 'research'


def test_gate_growth_signal_only_does_not_automatically_allocate_research():
    funding_only = group_events_by_company([event_fixture(company_name='Seed Startup',
        event_type='funding', title='Seed Startupがシード資金を調達', strength=100,
        possible_video_need='資金調達後の採用・事業紹介映像の需要は不明')])[0]
    low_win = CheapWinOutput(win_pre=4, confidence='low', hard_blocker=False,
        risk_tags=[], unknown_factors=['購買経路不明', '予算不明'], reason='情報不足')
    assert gate_status(low_win, Settings(), funding_only) == 'hold'


def test_gate_simple_new_service_is_not_a_communication_trigger_by_label_alone():
    simple_service = group_events_by_company([event_fixture(event_type='new_service_launch',
        title='既存サービスに小機能を追加', strength=50)])[0]
    result = CheapWinOutput(win_pre=4, confidence='low', hard_blocker=False,
        risk_tags=[], unknown_factors=['購買経路不明'], reason='追加確認が必要')
    assert gate_status(result, Settings(), simple_service) == 'hold'

    generic_launch = group_events_by_company([event_fixture(event_type='new_service_launch',
        title='新サービスを提供開始', strength=50)])[0]
    assert gate_status(result, Settings(), generic_launch) == 'hold'


def test_zero_strength_does_not_route_a_complex_offer_from_semantics_alone():
    event = event_fixture(event_type='announcement', title='AIインフラサービス提供開始', strength=0)
    opportunity = group_events_by_company([event])[0]
    result = CheapWinOutput(win_pre=4, confidence='low', hard_blocker=False,
        risk_tags=[], unknown_factors=['購買経路不明'], reason='追加確認が必要')
    assert classify_event_semantics(event).canonical_types == ('new_service',)
    assert gate_status(result, Settings(), opportunity) == 'hold'


def test_gate_complex_new_business_can_be_researched_as_communication_trigger():
    complex_business = group_events_by_company([event_fixture(event_type='new_business_launch',
        title='法人向けAIの新事業を新しいターゲット市場へ展開',
        strength=50)])[0]
    ai_service = group_events_by_company([event_fixture(company_name='AI Service Inc.',
        event_type='new_service_launch', title='AIエージェントサービスを提供開始', strength=50)])[0]
    result = CheapWinOutput(win_pre=4, confidence='low', hard_blocker=False,
        risk_tags=[], unknown_factors=['購買経路不明'], reason='確認が必要')
    assert gate_status(result, Settings(), complex_business) == 'research'
    assert gate_status(result, Settings(), ai_service) == 'research'


@pytest.mark.parametrize('title', [
    'AIインフラサービス提供開始',
    '生成AI導入支援ソリューション提供開始',
    'AIエージェント開発支援サービス提供開始',
    'フィジカルAI提供開始',
    'AIワークスペース正式公開',
    'AIサイバーセキュリティ支援サービスを開始',
])
def test_gate_recognizes_complex_service_launch_from_title_not_only_free_type(title):
    event = event_fixture(event_type='announcement', title=title, strength=80)
    semantics = classify_event_semantics(event)
    opportunity = group_events_by_company([event])[0]
    result = CheapWinOutput(win_pre=4, confidence='low', hard_blocker=False,
        risk_tags=[], unknown_factors=['購買経路不明'], reason='Gateで調査価値を判断')
    assert 'new_service' in semantics.canonical_types
    assert 'complex_service' in semantics.strong_communication_signals
    assert gate_status(result, Settings(), opportunity) == 'research'


@pytest.mark.parametrize(('event_type', 'title', 'canonical', 'not_canonical'), [
    ('announcement', 'AI企業との業務提携を発表', 'partnership', 'new_service'),
    ('announcement', '新サービスに関するセミナーを開催', 'event', 'new_service'),
    ('announcement', 'ブランド企業との共同キャンペーンを開始', 'campaign', 'rebrand'),
    ('announcement', '生成AIサービスの料金体系変更', 'pricing_update', 'new_service'),
    ('new_service_launch', '既存機能をAIエージェント化', 'feature_update', 'new_service'),
])
def test_gate_event_semantics_respect_partnership_event_campaign_and_update_context(
        event_type, title, canonical, not_canonical):
    event = event_fixture(event_type=event_type, title=title, strength=80)
    semantics = classify_event_semantics(event)
    assert canonical in semantics.canonical_types
    assert not_canonical not in semantics.canonical_types
    assert not researchable_event_signals(group_events_by_company([event])[0], Settings())


def test_gate_ipo_needs_strength_and_a_specific_communication_hypothesis():
    settings = Settings(gate_research_event_strength=90)
    result = CheapWinOutput(win_pre=4, confidence='low', hard_blocker=False,
        risk_tags=[], unknown_factors=['購買経路不明'], reason='現在の購買可能性は不明')
    low_strength = group_events_by_company([event_fixture(event_type='ipo', title='東京証券取引所へ上場', strength=89,
        possible_video_need='上場後に事業内容を説明する映像の需要は未確認')])[0]
    no_communication_hypothesis = group_events_by_company([event_fixture(event_type='ipo', title='東京証券取引所へ上場', strength=100,
        possible_video_need='上場した')])[0]
    strong_relevant_ipo = group_events_by_company([event_fixture(event_type='ipo', title='東京証券取引所へ上場', strength=100,
        possible_video_need='上場後の事業内容を企業顧客と採用候補者へ説明する映像需要は未確認')])[0]
    assert gate_status(result, settings, low_strength) == 'hold'
    assert gate_status(result, settings, no_communication_hypothesis) == 'hold'
    assert gate_status(result, settings, strong_relevant_ipo) == 'research'


def test_gate_strong_event_strength_cutoff_is_configurable():
    opportunity = group_events_by_company([event_fixture(event_type='ipo', title='東京証券取引所へ上場', strength=80,
        possible_video_need='上場後の事業内容を企業顧客へ説明する映像の需要は未確認')])[0]
    result = CheapWinOutput(win_pre=4, confidence='low', hard_blocker=False, risk_tags=[],
        unknown_factors=['購買経路不明'], reason='WIN confidence low')
    assert gate_status(result, Settings(gate_research_event_strength=80), opportunity) == 'research'
    assert gate_status(result, Settings(gate_research_event_strength=81), opportunity) == 'hold'


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
        event = EventInterpretation(company_name='株式会社ABC', event_type='investment', title=raw_item['title'],
            summary='株式会社ABCに出資')
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
    assert events[0].source_name == 'Incubate Fund'
    assert str(events[0].source_url) == raw_item.source_url
    assert events[0].evidence[0].raw_item_id == raw_item.id
    assert events[0].strength == 100
    assert processed_ids == {1} and rejected == 0
    assert client.calls[0][1]['use_web_search'] is False


def test_raw_item_payload_preserves_all_source_category_metadata():
    raw_item = RawItem(id=1, source_type='vc_news', source_name='VC', event_type='vc_news',
        company_name='株式会社ABC', title='投資家ランキング', summary='', published_at=None,
        source_url='https://vc.example/news/1', raw_data={'categories': ['メディア掲載', '掲載']})
    payload = raw_item_payload(raw_item, evaluate_category_hold())
    assert payload['source_categories'] == ['メディア掲載', '掲載']


def evaluate_category_hold():
    from app.prefilters import PrefilterDecision
    return PrefilterDecision('HOLD', 'source_category: media', 0, 'test', 'media', 10)


def test_event_llm_payloads_do_not_request_code_owned_source_metadata():
    assert not {'source_type', 'source_name', 'evidence', 'raw_item_id'} & EventInterpretation.model_fields.keys()
    assert not {'source_type', 'source_name', 'evidence'} & WebEventInterpretation.model_fields.keys()
    assert {'source_url', 'source_title'} <= WebEventInterpretation.model_fields.keys()


class EmptyWebEventClient:
    def prompt(self, _name):
        return 'discovery fixture'

    async def generate(self, *, output_type, **_kwargs):
        assert output_type is EventDiscoveryOutput
        return Generation(EventDiscoveryOutput(events=[]), set())


def test_web_discovery_accepts_zero_events_without_filling_count():
    events, rejections = asyncio.run(discover_web_events(EmptyWebEventClient(), Settings(), topics=['test']))
    assert events == [] and rejections == []


class WebEventFixtureClient:
    def prompt(self, _name):
        return 'discovery fixture'

    async def generate(self, *, output_type, **_kwargs):
        assert output_type is EventDiscoveryOutput
        event = WebEventInterpretation(company_name='株式会社ABC', event_type='funding',
            title='Series A資金調達', summary='資金調達を発表', published_at='2026-09-10',
            source_url='https://source.example/funding', source_title='公式発表',
            possible_video_need='')
        return Generation(EventDiscoveryOutput(events=[event]), {'https://source.example/funding'})


def test_web_event_source_identity_is_injected_from_code():
    events, rejections = asyncio.run(discover_web_events(WebEventFixtureClient(), Settings(), topics=['test']))
    assert rejections == [] and len(events) == 1
    assert events[0].source_type == 'web_search' and events[0].source_name == 'Web Search'
    assert events[0].evidence[0].source_title == '公式発表'
    assert events[0].possible_video_need == ''


class WebEventRejectionFixtureClient:
    def prompt(self, _name):
        return 'discovery fixture'

    async def generate(self, *, output_type, **_kwargs):
        assert output_type is EventDiscoveryOutput
        base = dict(event_type='announcement', title='企業変化を発表', summary='',
                    source_title='発表資料', possible_video_need='')
        events = [
            WebEventInterpretation(company_name='未確認URL社', source_url='https://untrusted.example/a', **base),
            WebEventInterpretation(company_name='未来日社', source_url='https://trusted.example/future',
                published_at=date.today() + timedelta(days=1), **base),
            WebEventInterpretation(company_name='未確認事実社', source_url='https://trusted.example/fact',
                research_facts=[ResearchFact(topic='change', fact='未確認事実',
                    source_url='https://untrusted.example/fact')], **base),
            WebEventInterpretation(company_name='  ', source_url='https://trusted.example/blank', **base),
            WebEventInterpretation(company_name='採用社', source_url='https://trusted.example/accepted', **base),
            WebEventInterpretation(company_name='空タイトル社', source_url='https://trusted.example/invalid',
                **(base | {'title': '  '})),
        ]
        return Generation(EventDiscoveryOutput(events=events), {
            'https://trusted.example/future', 'https://trusted.example/fact',
            'https://trusted.example/blank', 'https://trusted.example/accepted', 'https://trusted.example/invalid'})


def test_web_event_rejections_have_stable_reasons_and_details():
    events, rejections = asyncio.run(discover_web_events(
        WebEventRejectionFixtureClient(), Settings(), topics=['test']))
    reasons = Counter(item.reason for item in rejections)
    assert len(events) == 1 and events[0].company_name == '採用社'
    assert len(rejections) == 5
    assert reasons == Counter({
        'unverified_source_url': 1,
        'future_published_date': 1,
        'unverified_fact_url': 1,
        'missing_company_identity': 1,
        'invalid_event': 1,
    })
    untrusted_source = next(item for item in rejections if item.reason == 'unverified_source_url')
    assert untrusted_source.company_name == '未確認URL社'
    assert untrusted_source.title == '企業変化を発表'
    assert untrusted_source.source_url == 'https://untrusted.example/a'
    assert rejections[2].field == 'research_facts.source_url'


class DiagnosticEvidenceClient:
    def __init__(self, output, evidence_urls):
        self.output = output
        self.evidence_urls = evidence_urls

    def prompt(self, _name):
        return 'diagnostic fixture'

    async def generate(self, *, output_type, **_kwargs):
        assert output_type is DiagnosticOutput
        return Generation(self.output, self.evidence_urls)


def test_diagnostic_evidence_must_match_retrieved_or_input_sources():
    opportunity = group_events_by_company([event_fixture(source_url='https://source.example/event')])[0]
    valid_claim = ResearchEvidence(claim='公式サイト上でサービス映像を確認', evidence_type='observed',
        source_url='https://research.example/service', confidence='high')
    valid_output = diagnostic_output().model_copy(update={'evidence': [valid_claim]})
    result = asyncio.run(run_diagnostic_research(DiagnosticEvidenceClient(
        valid_output, {'https://research.example/service'}), Settings(), opportunity, {}, []))
    assert result.value.evidence[0].claim == valid_claim.claim

    invented_claim = ResearchEvidence(claim='制作会社のCreditを確認', evidence_type='observed',
        source_url='https://invented.example/credit', confidence='high')
    invalid_output = diagnostic_output().model_copy(update={'evidence': [invented_claim]})
    result = asyncio.run(run_diagnostic_research(DiagnosticEvidenceClient(
        invalid_output, {'https://research.example/service'}), Settings(), opportunity, {}, []))
    assert result.value.evidence[0].evidence_type == 'unknown'
    assert result.value.evidence[0].source_url is None
    assert result.warnings == ['untrusted evidence URL removed: evidence[0].source_url']


def test_unknown_research_evidence_has_no_source_and_is_not_a_negative_fact():
    unknown = ResearchEvidence(claim='既存の採用映像があるか未確認', evidence_type='unknown',
        source_url=None, confidence='low')
    assert unknown.evidence_type == 'unknown' and unknown.source_url is None
    with pytest.raises(ValueError, match='Unknown evidence must not claim'):
        ResearchEvidence(claim='採用映像が存在しない', evidence_type='unknown',
            source_url='https://example.com', confidence='low')


def test_diagnostic_failure_categories_are_stable_and_do_not_expose_raw_output():
    schema_error = InvalidOutputError('Invalid model output after 3 attempts',
        validation_type='schema_validation', field_errors=[
            {'field': 'current_expression.assets.0.url', 'type': 'url_parsing', 'expected': 'valid URL'}])
    category, details = classify_diagnostic_failure(schema_error)
    assert category == 'diagnostic_schema_validation'
    assert details['field_errors'][0]['field'] == 'current_expression.assets.0.url'
    assert 'raw' not in json.dumps(details).lower()

    missing_evidence = InvalidOutputError('Invalid model output after 3 attempts',
        validation_type='schema_validation', field_errors=[
            {'field': 'evidence', 'type': 'too_short', 'expected': 'at least 1 item'}])
    assert classify_diagnostic_failure(missing_evidence)[0] == 'diagnostic_missing_evidence'
    assert classify_diagnostic_failure(InvalidOutputError('invalid response',
        validation_type='output_validation'))[0] == 'diagnostic_output_validation'
    assert classify_diagnostic_failure(TimeoutError())[0] == 'diagnostic_timeout'
    assert classify_diagnostic_failure(ValueError('unclassified validation'))[0] == 'diagnostic_unknown_error'
    assert safe_diagnostic_url('https://user:pass@research.example/path?token=secret#fragment') == \
        'https://research.example/path'


def test_research_stage_saves_categorized_schema_failure_and_holds_opportunity():
    from app.opportunity_stages import research_opportunities

    class InvalidDiagnosticClient:
        def prompt(self, _name):
            return 'diagnostic fixture'

        async def generate(self, **_kwargs):
            raise InvalidOutputError('Invalid model output after 3 attempts',
                validation_type='schema_validation', field_errors=[
                    {'field': 'evidence', 'type': 'missing', 'expected': 'field required'}])

    opportunity = group_events_by_company([event_fixture()])[0]
    opportunity.status = 'research'
    opportunity.gate = {'win_pre': {'win_pre': 4, 'confidence': 'low'}}
    errors = []
    asyncio.run(research_opportunities([opportunity], InvalidDiagnosticClient(), Settings(), [], errors))
    assert opportunity.status == 'hold'
    assert errors == [('diagnostic', opportunity.company_name, 'diagnostic_missing_evidence')]


def test_research_unknown_error_keeps_sanitized_message_and_logs_stack(caplog):
    from app.opportunity_stages import research_opportunities

    class BrokenDiagnosticClient:
        def prompt(self, _name):
            return 'diagnostic fixture'

        async def generate(self, **_kwargs):
            raise TypeError('bad response shape; Authorization: sk-test-secret-value')

    opportunity = group_events_by_company([event_fixture()])[0]
    opportunity.status = 'research'
    opportunity.gate = {'win_pre': {'win_pre': 6, 'confidence': 'medium'}}
    errors, details = [], {}
    with caplog.at_level('ERROR'):
        asyncio.run(research_opportunities([opportunity], BrokenDiagnosticClient(), Settings(), [], errors, details))
    key = ('diagnostic', opportunity.company_name, 'diagnostic_unknown_error')
    assert details[key]['message'].startswith('bad response shape')
    assert details[key]['exception_type'] == 'TypeError'
    assert 'sk-test-secret' not in details[key]['message']
    assert 'diagnostic unexpected exception' in caplog.text
    assert 'sk-test-secret' not in caplog.text


def test_untrusted_diagnostic_url_is_downgraded_without_research_failure():
    from app.diagnostics import run_diagnostic_research

    class DiagnosticClient:
        def prompt(self, _name):
            return 'diagnostic fixture'

        async def generate(self, **_kwargs):
            value = diagnostic_output().model_copy(update={'evidence': [ResearchEvidence(
                claim='モデルが生成した未検証URLの主張', evidence_type='observed',
                source_url='https://untrusted.example/generated', confidence='high')]})
            return Generation(value, {'https://search.example/result'})

    opportunity = group_events_by_company([event_fixture()])[0]
    result = asyncio.run(run_diagnostic_research(DiagnosticClient(), Settings(), opportunity, {}, []))
    assert result.value.evidence[0].source_url is None
    assert result.value.evidence[0].evidence_type == 'unknown'
    assert result.evidence_urls == {'https://search.example/result'}
    assert result.warnings == ['untrusted evidence URL removed: evidence[0].source_url']


class ScoreCaptureClient:
    def __init__(self, output):
        self.output = output
        self.input_text = None

    def prompt(self, _name):
        return 'scoring fixture'

    async def generate(self, *, input_text, output_type, **_kwargs):
        self.input_text = input_text
        assert output_type is EvaluationOutput
        return Generation(self.output, set())


def test_score_receives_research_claims_and_rejects_untrusted_risk_urls():
    from app.opportunity_stages import score_opportunities

    opportunity = group_events_by_company([event_fixture(source_url='https://source.example/event')])[0]
    opportunity.status = 'researched'
    opportunity.research = diagnostic_output().model_copy(update={'evidence': [ResearchEvidence(
        claim='新サービス映像を確認', evidence_type='observed',
        source_url='https://research.example/service', confidence='high')]})
    opportunity.research_evidence_urls = ['https://abc.example', 'https://research.example/service']
    opportunity.gate = {'vc_profile_context': [{'name': 'VC', 'evidence': ['https://vc.example/profile']}]}

    good_client = ScoreCaptureClient(evaluation_output())
    asyncio.run(score_opportunities([opportunity], good_client, Settings(), []))
    scoring_context = json.loads(good_client.input_text.split('V2_DIAGNOSTIC_CONTEXT=', 1)[1])
    assert scoring_context['research']['evidence'][0]['source_url'] == 'https://research.example/service'
    assert scoring_context['research_evidence_urls'] == ['https://abc.example', 'https://research.example/service']
    assert scoring_context['gate']['vc_profile_context'][0]['name'] == 'VC'

    opportunity.status = 'researched'
    opportunity.score = None
    invalid_risk = RiskAssessment(risk='固定制作パートナーが継続', axis='win', dimension='competitive_openness',
        severity='high', evidence_type='observed', evidence='公式Creditが複数案件で継続',
        source_urls=['https://invented.example/credit'], score_impact='material_decrease',
        score_impact_reason='競争上の参入余地へ反映')
    invalid_score = evaluation_output().model_copy(update={'risks': [invalid_risk]})
    errors = []
    asyncio.run(score_opportunities([opportunity], ScoreCaptureClient(invalid_score), Settings(), errors))
    assert opportunity.status == 'hold' and opportunity.score is None
    assert errors == [('scoring', opportunity.company_name, 'ValueError')]


def test_strategy_receives_completed_scores_without_owning_them():
    from app.strategy import generate_strategy

    class StrategyCaptureClient:
        input_text = None

        def prompt(self, _name):
            return 'strategy fixture'

        async def generate(self, *, input_text, output_type, **_kwargs):
            self.input_text = input_text
            assert output_type is StrategyOutput
            value = StrategyOutput(why_now='新事業の開始', business_context='技術サービス',
                video_problem_hypothesis='説明手段の仮説',
                proposal={'title': '事業紹介映像', 'description': '技術を説明', 'deliverables': ['60秒']},
                estimated_budget='30万円前後の提案仮説', target_department='事業開発（仮説）',
                target_role='担当責任者（仮説）', first_contact_method='公式窓口（仮説）',
                sales_angle='事業の説明', risks=[], research_notes=[])
            return Generation(value, set())

    context = {'status': 'scored', 'gate': {'status': 'research'},
               'score': evaluation_output().model_dump(mode='json'),
               'research_evidence_urls': ['https://research.example/evidence']}
    client = StrategyCaptureClient()
    asyncio.run(generate_strategy(client, Settings(), fixed_candidate('https://source.example'),
                                  75.0, strategy_context=context))
    passed_context = json.loads(client.input_text.split('V2_OPPORTUNITY_CONTEXT=', 1)[1])
    assert passed_context == context


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
            event = EventInterpretation(company_name='株式会社ABC', event_type='investment', title=raw_item['title'],
                summary='技術サービスへの出資', published_at=raw_item['published_at'],
                strength=raw_item['event_strength'], location='東京都',
                possible_video_need='技術サービスのブランド映像')
            return Generation(FixedEventOutput(events=[FixedEventItem(event=event,
                raw_item_ids=[raw_item['id']])]), set())
        if output_type is CheapWinBatchOutput:
            companies = json.loads(input_text)['companies']
            return Generation(CheapWinBatchOutput(results=[
                CheapWinBatchResult(win_pre=6.5, confidence='medium', hard_blocker=False,
                    risk_tags=[], unknown_factors=['購入経路詳細'],
                    reason='小規模の提案余地がある', decision='RESEARCH',
                    trigger_quality='strong', target_fit='good', research_value='high',
                    company_id=item['company_id'], company_name=item['company_name'])
                for item in companies]), set())
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
        record = session.scalar(select(CandidateRecord))
        assert record.status == 'scored'
        assert record.win_pre_json['unknown_factors'] == ['購入経路詳細']
        assert record.merged_json['research_evidence_urls'] == ['https://abc.example']
        assert record.merged_json['research']['evidence'][0]['claim'] == 'Discoveryで確認した企業Event'
        assert session.scalar(select(func.count()).select_from(Diagnostic)) == 1
        assert session.scalar(select(func.count()).select_from(ResearchScore)) == 1
        assert session.scalar(select(ResearchScore)).selected_rank == 1
    fixed_input = next(json.loads(input_text) for output, _kwargs, input_text in client.calls
                       if output is FixedEventOutput)
    assert [item['title'] for item in fixed_input['raw_items']] == ['株式会社ABCへ出資']
    diagnostic_call = next(kwargs for output, kwargs, _input in client.calls if output is DiagnosticOutput)
    assert diagnostic_call['use_web_search'] is True
    scoring_input = next(input_text for output, _kwargs, input_text in client.calls
                         if output is EvaluationOutput)
    assert 'research_evidence_urls' in scoring_input
    assert 'vc_profile_context' in scoring_input
    factory.kw['bind'].dispose()


def test_web_event_rejections_are_observable_but_not_run_errors(tmp_path, caplog):
    caplog.set_level('INFO')
    class RejectedWebOnlyClient:
        def prompt(self, _name):
            return 'discovery fixture'

        async def generate(self, *, output_type, **_kwargs):
            assert output_type is EventDiscoveryOutput
            event = WebEventInterpretation(company_name='ABC', event_type='announcement',
                title='変化を発表', summary='', source_url='https://untrusted.example/news',
                source_title='記事')
            return Generation(EventDiscoveryOutput(events=[event]), set())

    settings = Settings(openai_api_key='fake-test', database_url=f'sqlite:///{tmp_path / "db"}')
    assert asyncio.run(run_daily_pipeline(settings, RejectedWebOnlyClient(),
        web_discovery=True, fixed_discovery=False, topics=['fixture']))
    factory = init_database(settings.database_url)
    with factory() as session:
        run = session.scalar(select(Run))
        assert run.status == 'completed'
        assert session.scalar(select(func.count()).select_from(ProcessingError)) == 0
    assert 'accepted=0 rejected=1 rejection_reasons={\'unverified_source_url\': 1}' in caplog.text
    factory.kw['bind'].dispose()


def test_fixed_validation_rejection_is_warning_not_partial_run(tmp_path, monkeypatch):
    async def rejected_fixed_events(*_args, **_kwargs):
        return [], {}, set(), 1

    monkeypatch.setattr('app.v2_pipeline.extract_fixed_events', rejected_fixed_events)
    settings = Settings(openai_api_key='fake-test', database_url=f'sqlite:///{tmp_path / "db"}')
    assert asyncio.run(run_daily_pipeline(settings, V2MockClient(),
        web_discovery=False, fixed_discovery=True))
    factory = init_database(settings.database_url)
    with factory() as session:
        run = session.scalar(select(Run))
        assert run.status == 'completed'
        assert run.config_json['summary']['validation_warnings'] == 1
        assert session.scalar(select(func.count()).select_from(ProcessingError)) == 0
    factory.kw['bind'].dispose()


def test_web_discovery_timeout_keeps_run_partial_instead_of_failed(tmp_path):
    class TimeoutWebClient:
        def prompt(self, _name):
            return 'discovery fixture'

        async def generate(self, *, output_type, **_kwargs):
            assert output_type is EventDiscoveryOutput
            raise APITimeoutError(request=httpx.Request('POST', 'https://api.openai.com'))

    settings = Settings(openai_api_key='fake-test', database_url=f'sqlite:///{tmp_path / "db"}')
    assert not asyncio.run(run_daily_pipeline(settings, TimeoutWebClient(),
        web_discovery=True, fixed_discovery=False, topics=['fixture']))
    factory = init_database(settings.database_url)
    with factory() as session:
        run = session.scalar(select(Run))
        error = session.scalar(select(ProcessingError))
        assert run.status == 'partial'
        assert error.stage == 'discovery'
        assert error.error_type == 'APITimeoutError'
    factory.kw['bind'].dispose()


def test_diagnostic_failure_category_is_persisted_without_raw_exception(tmp_path):
    class InvalidResearchPipelineClient(V2MockClient):
        async def generate(self, *, output_type, input_text='', **kwargs):
            if output_type is DiagnosticOutput:
                raise InvalidOutputError('Invalid model output after 3 attempts',
                    validation_type='schema_validation', field_errors=[
                        {'field': 'evidence', 'type': 'missing', 'expected': 'field required'}])
            return await super().generate(output_type=output_type, input_text=input_text, **kwargs)

    async def fake_collector(_settings, factory):
        with factory.begin() as session:
            session.add(SourceEvent(source_type='vc_news', source_name='Incubate Fund',
                event_type='investment', company_name='株式会社ABC', title='株式会社ABCへ出資',
                summary='事業に出資', source_url='https://vc.example/news/1', external_id='investment-1',
                raw_data={'category': '投資'}))
        return CollectionResult([], [])

    settings = Settings(openai_api_key='fake-test', database_url=f'sqlite:///{tmp_path / "db"}')
    assert not asyncio.run(run_v2_pipeline(settings, InvalidResearchPipelineClient(),
        web_discovery=False, collector=fake_collector))
    factory = init_database(settings.database_url)
    with factory() as session:
        run = session.scalar(select(Run))
        error = session.scalar(select(ProcessingError))
        assert run.status == 'partial'
        assert error.stage == 'diagnostic'
        assert error.error_type == 'diagnostic_missing_evidence'
        assert error.exception_type == 'DiagnosticFailure'
        assert error.error_message
        assert 'Invalid model output' not in (run.error_message or '')
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
