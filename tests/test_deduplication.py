from sqlalchemy import select, func
from app.deduplication import persist_candidate, normalize_name
from app.models import Company, Trigger


def save(session, candidate, run_id):
    return persist_candidate(session, candidate, run_id, 'test-model', 'v1', {str(candidate.source_url)})


def test_same_domain(database, candidate):
    factory, run_id = database
    with factory.begin() as session:
        first, _, _ = save(session, candidate, run_id)
        changed = candidate.model_copy(update={'company_name': 'ABC株式会社', 'website': 'https://example.com/about'})
        second, _, is_new = save(session, changed, run_id)
        assert first.id == second.id
        assert not is_new
        assert session.scalar(select(func.count()).select_from(Company)) == 1


def test_new_trigger(database, candidate):
    factory, run_id = database
    with factory.begin() as session:
        first, _, _ = save(session, candidate, run_id)
        changed = candidate.model_copy(update={'trigger_title': '採用強化', 'trigger_type': 'hiring',
                                               'source_url': 'https://example.com/news/2'})
        second, _, is_new = save(session, changed, run_id)
        assert first.id == second.id and is_new
        assert session.scalar(select(func.count()).select_from(Trigger)) == 2


def test_name_fallback(database, candidate):
    factory, run_id = database
    with factory.begin() as session:
        first, _, _ = save(session, candidate.model_copy(update={'website': None}), run_id)
        second, _, _ = save(session, candidate.model_copy(update={'company_name': 'ABC 株式会社'}), run_id)
        assert first.id == second.id
        assert second.website_domain == 'example.com'


def test_conflicting_domains_not_merged(database, candidate):
    factory, run_id = database
    with factory.begin() as session:
        first, _, _ = save(session, candidate, run_id)
        second, _, _ = save(session, candidate.model_copy(update={'website': 'https://different.example'}), run_id)
        assert first.id != second.id


def test_normalize():
    assert normalize_name('株式会社 ABC') == normalize_name('ＡＢＣ株式会社') == 'abc'
