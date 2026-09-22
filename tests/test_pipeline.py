import asyncio
from unittest.mock import AsyncMock
from pydantic import HttpUrl
from sqlalchemy import select, func
from app.config import Settings
from app.database import init_database
from app.llm import Generation
from app.models import Run, Company, ResearchScore as Score, Strategy, ProcessingError
from app.pipeline import run_pipeline
from app.schemas import EvaluationOutput, StrategyOutput, DiscoveryOutput
from app.scoring import WEIGHTS
from app.discovery import discover_topic


def evaluation():
    from app.schemas import NeedScores, WinScores, DeliverScores
    return EvaluationOutput(
        need=dict.fromkeys(NeedScores.model_fields, 8),
        win=dict.fromkeys(WinScores.model_fields, 5),
        deliver=dict.fromkeys(DeliverScores.model_fields, 5),
        scope_hypothesis='1拠点60秒の映像という仮説',
        evidence_coverage={stage: {'coverage': 'low', 'reason': '未確認が多い'}
                           for stage in ('need', 'win', 'deliver')},
        reason='NEEDは変化、WINとDELIVERは不明', strongest_signals=[], risks=[], research_needed=[])


class FakeClient:
    def prompt(self, name):
        return name

    async def generate(self, *, output_type, input_text, **kwargs):
        if output_type is EvaluationOutput:
            if 'Failure' in input_text:
                raise RuntimeError('API secret that must not be persisted')
            return Generation(evaluation(), set())
        return Generation(StrategyOutput(why_now='今', business_context='context',
            video_problem_hypothesis='hypothesis', proposal={'title': 'brand', 'description': 'film',
            'deliverables': ['60秒']}, estimated_budget='30〜50万円', target_department='広報',
            target_role='責任者', first_contact_method='問い合わせフォーム', sales_angle='認知',
            risks=[], research_notes=[]), set())


def test_pipeline_isolates_failure_and_skips_history(tmp_path, candidate, monkeypatch):
    settings = Settings(openai_api_key='fake-test', database_url=f'sqlite:///{tmp_path / "db"}')
    bad = candidate.model_copy(update={'company_name': 'Failure', 'website': HttpUrl('https://failure.example')})
    good = candidate.model_copy(update={'company_name': 'Success C', 'website': HttpUrl('https://c.example')})
    discovery = AsyncMock(return_value=([candidate, bad, good], {str(candidate.source_url)}, 0))
    monkeypatch.setattr('app.pipeline.DISCOVERY_TOPICS', ['test'])
    monkeypatch.setattr('app.pipeline.discover_topic', discovery)
    notification = AsyncMock()
    monkeypatch.setattr('app.pipeline.send_discord_report', notification)
    assert not asyncio.run(run_pipeline(settings, FakeClient()))
    factory = init_database(settings.database_url)
    with factory() as session:
        run = session.scalar(select(Run))
        assert (run.status, run.candidate_count, run.scored_count, run.strategy_count) == ('partial', 3, 2, 2)
        assert session.scalar(select(func.count()).select_from(Score)) == 2
        assert session.scalar(select(func.count()).select_from(Strategy)) == 2
        assert session.scalar(select(ProcessingError)).error_type == 'RuntimeError'
        assert 'secret' not in run.error_message
        assert set(session.scalars(select(Score.selected_rank))) == {1, 2}
    # One alert for the partial failure and one normal result report.
    assert notification.await_count == 2
    asyncio.run(run_pipeline(settings, FakeClient()))
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == 3
        assert session.scalar(select(func.count()).select_from(Score)) == 2
    factory.kw['bind'].dispose()


def test_dummy_key_records_failure(tmp_path):
    settings = Settings(database_url=f'sqlite:///{tmp_path / "db"}')
    assert not asyncio.run(run_pipeline(settings))
    factory = init_database(settings.database_url)
    with factory() as session:
        run = session.scalar(select(Run))
        assert run.status == 'failed' and run.finished_at is not None
    factory.kw['bind'].dispose()


def test_discovery_rejects_unverified_and_malformed(candidate):
    raw = candidate.model_dump(mode='json')
    invented = dict(raw, source_url='https://invented.example')
    client = FakeClient()
    client.generate = AsyncMock(return_value=Generation(
        DiscoveryOutput(candidates=[raw, invented, {'company_name': 'invalid'}]), {str(candidate.source_url)}))
    candidates, _, rejected = asyncio.run(discover_topic(client, Settings(), 'test'))
    assert len(candidates) == 1 and rejected == 2
    assert candidates[0].discovered_at.tzinfo is not None


def test_scoring_only_cache_replay(tmp_path, candidate, monkeypatch):
    import json
    settings = Settings(openai_api_key='fake-test', database_url=f'sqlite:///{tmp_path / "db"}')
    cache = tmp_path / 'discovery.json'
    discover = AsyncMock(return_value=([candidate], {str(candidate.source_url)}, 0))
    strategy = AsyncMock(side_effect=AssertionError('Strategy must not run'))
    notify = AsyncMock(side_effect=AssertionError('Notification must not run'))
    monkeypatch.setattr('app.pipeline.discover_topic', discover)
    monkeypatch.setattr('app.pipeline.generate_strategy', strategy)
    monkeypatch.setattr('app.pipeline.send_discord_report', notify)
    assert asyncio.run(run_pipeline(settings, FakeClient(), scoring_only=True,
                                    topics=['test'], discovery_cache=cache))
    assert len(json.loads(cache.read_text())) == 1
    assert asyncio.run(run_pipeline(settings, FakeClient(), scoring_only=True, replay=cache))
    assert discover.await_count == 1
    strategy.assert_not_awaited()
    notify.assert_not_awaited()
    factory = init_database(settings.database_url)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Score)) == 2
        assert all(run.strategy_count == 0 for run in session.scalars(select(Run)))
        assert session.scalar(select(Score)).evaluation_json['need']['identity_shift'] == 8
    factory.kw['bind'].dispose()


def test_scoring_report_sends_without_strategy(tmp_path, candidate, monkeypatch):
    settings = Settings(openai_api_key='fake-test', database_url=f'sqlite:///{tmp_path / "db"}')
    discover = AsyncMock(return_value=([candidate], {str(candidate.source_url)}, 0))
    strategy = AsyncMock(side_effect=AssertionError('Strategy must not run'))
    notification = AsyncMock()
    monkeypatch.setattr('app.pipeline.discover_topic', discover)
    monkeypatch.setattr('app.pipeline.generate_strategy', strategy)
    monkeypatch.setattr('app.pipeline.send_discord_report', notification)
    assert asyncio.run(run_pipeline(settings, FakeClient(), scoring_report=True, topics=['test']))
    strategy.assert_not_awaited()
    notification.assert_awaited_once()
    factory = init_database(settings.database_url)
    with factory() as session:
        run = session.scalar(select(Run))
        assert run.strategy_count == 0
        assert run.config_json['scoring_report'] is True
    factory.kw['bind'].dispose()


def test_discovery_rejects_unverified_research(candidate):
    raw = candidate.model_dump(mode='json')
    raw['research_facts'] = [{'topic': 'sns', 'fact': '公式アカウントあり',
                             'source_url': 'https://invented.example/'}]
    client = FakeClient()
    client.generate = AsyncMock(return_value=Generation(DiscoveryOutput(candidates=[raw]),
                                                        {str(candidate.source_url)}))
    accepted, _, rejected = asyncio.run(discover_topic(client, Settings(), 'test'))
    assert accepted == [] and rejected == 1


def test_scoring_uses_new_schema_without_search(candidate):
    from app.scoring import score_company
    client = FakeClient()
    client.generate = AsyncMock(return_value=Generation(evaluation(), set()))
    result = asyncio.run(score_company(client, Settings(), candidate))
    assert result.need.identity_shift == 8
    kwargs = client.generate.call_args.kwargs
    assert kwargs['output_type'] is EvaluationOutput
    assert not kwargs.get('use_web_search', False)


def test_scoring_and_strategy_reasoning_are_configurable(candidate):
    from app.scoring import score_company
    from app.strategy import generate_strategy

    client = FakeClient()
    client.generate = AsyncMock(side_effect=[Generation(evaluation(), set()), Generation(
        StrategyOutput(why_now='今', business_context='context', video_problem_hypothesis='hypothesis',
        proposal={'title': 'brand', 'description': 'film', 'deliverables': ['60秒']},
        estimated_budget='30〜50万円', target_department='広報', target_role='責任者',
        first_contact_method='問い合わせフォーム', sales_angle='認知', risks=[], research_notes=[]), set())])
    settings = Settings(scoring_reasoning_effort='medium', strategy_reasoning_effort='high')

    asyncio.run(score_company(client, settings, candidate))
    asyncio.run(generate_strategy(client, settings, candidate, Score(total_score=80)))

    scoring_call, strategy_call = client.generate.call_args_list
    assert scoring_call.kwargs['reasoning_effort'] == 'medium'
    assert strategy_call.kwargs['reasoning_effort'] == 'high'
