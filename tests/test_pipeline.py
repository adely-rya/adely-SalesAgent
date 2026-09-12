import asyncio
from unittest.mock import AsyncMock
from pydantic import HttpUrl
from sqlalchemy import select, func
from app.config import Settings
from app.database import init_database
from app.llm import Generation
from app.models import Run, Company, Score, Strategy, ProcessingError
from app.pipeline import run_pipeline
from app.schemas import ScoreOutput, StrategyOutput, DiscoveryOutput
from app.scoring import WEIGHTS
from app.discovery import discover_topic


class FakeClient:
    def prompt(self, name):
        return name

    async def generate(self, *, output_type, input_text, **kwargs):
        if output_type is ScoreOutput:
            if 'Failure' in input_text:
                raise RuntimeError('API secret that must not be persisted')
            return Generation(ScoreOutput(**dict.fromkeys(WEIGHTS, 8), reason='ok', risks=[]), set())
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
    assert notification.await_count == 1
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
