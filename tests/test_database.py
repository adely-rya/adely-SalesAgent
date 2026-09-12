from sqlalchemy import select
from app.database import init_database
from app.models import Run, Company, Trigger, Score, Strategy, HumanRating
from app.deduplication import persist_candidate
from app.scoring import WEIGHTS


def test_persistence(tmp_path, candidate):
    url = f'sqlite:///{tmp_path / "sales.db"}'
    factory = init_database(url)
    with factory.begin() as session:
        run = Run()
        session.add(run)
        session.flush()
        company, trigger, _ = persist_candidate(session, candidate, run.id, 'discovery-model', 'v1',
                                                 {str(candidate.source_url)})
        session.add(Score(company_id=company.id, trigger_id=trigger.id, run_id=run.id,
            **dict.fromkeys(WEIGHTS, 8), total_score=80, reason='reason', risks_json=['risk'],
            model='score-model', prompt_version='v2', selected_rank=1))
        session.add(Strategy(company_id=company.id, trigger_id=trigger.id, run_id=run.id,
            content_json={'why_now': '今'}, model='strategy-model', prompt_version='v3', evidence_urls=[]))
        session.add(HumanRating(company_id=company.id, run_id=run.id, rating='good', comment='営業したい'))
    factory.kw['bind'].dispose()
    reopened = init_database(url)
    with reopened() as session:
        assert session.scalar(select(Company)).name == candidate.company_name
        assert session.scalar(select(Trigger)).model == 'discovery-model'
        assert session.scalar(select(Score)).selected_rank == 1
        assert session.scalar(select(Strategy)).content_json['why_now'] == '今'
        assert session.scalar(select(HumanRating)).rating == 'good'
    reopened.kw['bind'].dispose()
