from datetime import date
import pytest
from app.database import init_database
from app.models import Run
from app.schemas import Candidate


@pytest.fixture
def candidate():
    return Candidate(company_name='株式会社ABC', website='https://www.example.com',
        trigger_type='brand_launch', trigger_title='新ブランド開始', trigger_summary='新ブランドを発表',
        published_at=date(2026, 9, 10), source_url='https://example.com/news/1',
        source_title='公式発表', location='神奈川県', possible_video_need='ブランドムービー')


@pytest.fixture
def database(tmp_path):
    factory = init_database(f'sqlite:///{tmp_path / "sales.db"}')
    with factory.begin() as session:
        run = Run()
        session.add(run)
    yield factory, run.id
    factory.kw['bind'].dispose()
