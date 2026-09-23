import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock
from app.config import Settings
from app.report import (format_error_report, format_opportunity_messages, split_messages,
                        send_discord_report)
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


def test_error_report_groups_human_readable_stage_subjects():
    report = format_error_report(42, 'partial', [
        'diagnostic: キュリエ (ValueError)', 'scoring: エムティーアイ (ValueError)'])
    assert 'Diagnostic' in report and '・キュリエ' in report
    assert 'Scoring' in report and '・エムティーアイ' in report
    assert 'ValueError' not in report
    assert 'Details: server logs' in report


def test_opportunity_report_is_observed_first_and_hides_unknowns():
    from app.domain import Opportunity
    from app.schemas import (Candidate, CurrentExpression, DiagnosticOutput, EvaluationOutput,
                             Event, ExpressionDebt, CreativeLockIn, ResearchEvidence,
                             EvidenceCoverage, NeedScores, WinScores, DeliverScores,
                             StageEvidence)
    from datetime import date

    candidate = Candidate(company_name='湘南メーカー', website='https://example.com',
        trigger_type='site_renewal', trigger_title='採用サイトを刷新',
        trigger_summary='採用向けのサイトを刷新した', published_at=date(2026, 9, 10),
        source_url='https://example.com/news', source_title='公式発表', location='藤沢',
        possible_video_need='採用ブランド映像')
    event = Event(company_name='湘南メーカー', event_type='site_renewal', title='採用サイトを刷新',
        summary='採用サイトを刷新', published_at=date(2026, 9, 10), source_type='web_search',
        source_name='Web Search', source_url='https://example.com/news', source_title='公式発表',
        company_website='https://example.com', location='藤沢',
        research_facts=[], research_unknowns=[])
    research = DiagnosticOutput(
        current_expression=CurrentExpression(assets=[], summary='サービス説明はWeb中心', unknowns=['予算不明']),
        expression_debt=ExpressionDebt(score=7, business_change='採用サイトを刷新',
            expression_gap='採用候補者向けの動画表現が薄い', reason='観測事実', confidence='medium'),
        peer_gap=None,
        creative_lock_in=CreativeLockIn(status='unknown', partners_or_credits=[],
            observation='外部Partnerは未確認', evidence_confidence='low'),
        evidence=[ResearchEvidence(claim='採用サイトで事業内容と職種を説明 [参照](https://claim.example)',
            evidence_type='observed', source_url='https://example.com/recruit', confidence='high')])
    evaluation = EvaluationOutput(
        need=NeedScores(identity_shift=7, narrative_strength=7, communication_moment=8,
            expression_gap=7, visual_story_potential=7),
        win=WinScores(budget_likelihood=5, procurement_access=5, creative_investment=6,
            competitive_openness=6, proposal_fit=7),
        deliver=DeliverScores(production_scale_fit=8, capability_fit=8, quality_bar_fit=7,
            logistics_fit=9, operational_complexity_fit=8),
        scope_hypothesis='藤沢の現場と社員を使った60秒採用ブランド映像',
        evidence_coverage=EvidenceCoverage(
            need=StageEvidence(coverage='high', reason='採用サイト刷新と説明内容を確認'),
            win=StageEvidence(coverage='low', reason='外部予算は未確認'),
            deliver=StageEvidence(coverage='medium', reason='地域の少人数撮影と適合')),
        reason='Observed facts support a timely recruitment communication proposal.',
        strongest_signals=['採用サイト刷新'], risks=[], unknowns=['予算不明'], research_needed=[])
    opportunity = Opportunity(company_name='湘南メーカー', events=[event], candidate=candidate,
        origins=['web_search'], sources=[{'type': 'web_search', 'provider': 'Web Search',
            'url': 'https://example.com/news'}], status='scored', research=research,
        research_evidence_urls=['https://example.com/recruit', 'https://example.com/extra'],
        score=evaluation, final_score=68.4)
    from app.models import Run
    run = Run(id=11, status='partial', candidate_count=1, scored_count=1,
              config_json={'summary': {'opportunity_count': 1, 'gate': {'research': 1, 'hold': 0},
                  'research': {'success': 1, 'failed': 0}, 'scoring': {'success': 1, 'failed': 0},
                  'validation': {'evidence_urls_removed': 116, 'affected_companies': 3},
                  'runtime_seconds': 12.3}})
    messages = format_opportunity_messages(run, [opportunity], '2026-09-22')
    assert len(messages) == 2
    assert '116 evidence URLs removed automatically' in messages[0]
    assert 'Found via\n・Web Search' in messages[1]
    assert 'What we learned' in messages[1]
    assert '採用サイトで事業内容と職種を説明' in messages[1]
    assert '予算不明' not in messages[1]
    assert 'Research Sources' not in messages[1]
    assert messages[1].count('https://') <= 5
    assert '[参照](https://claim.example)' not in messages[1]


def test_legacy_validation_summary_keeps_the_count():
    from app.models import Run

    run = Run(id=11, status='partial', candidate_count=57,
              config_json={'summary': {'validation_warnings': 116}})
    summary = format_opportunity_messages(run, [], '2026-09-22')[0]
    assert '116 validation warnings summarized automatically' in summary
    assert 'Research continued normally' in summary


def test_schedule():
    now = datetime(2026, 9, 12, 7, 0, tzinfo=ZoneInfo('Asia/Tokyo'))
    assert daily_trigger(Settings()).get_next_fire_time(None, now).hour == 8
