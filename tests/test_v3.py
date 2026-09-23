import asyncio
from datetime import date

import pytest

from app.config import Settings
from app.database import init_database
from app.domain import Opportunity
from app.llm import Generation
from app.models import (V3CandidatePool, V3FinalSelection,
                        V3SalesMemo, V3ShortlistDecision)
from app.schemas import Event, EventEvidence
from app.v3_research import run_v3_sales_research
from app.v3_schemas import (V3Evidence, V3FinalSelectionItem,
                             V3FinalSelectorOutput, V3SalesMemoOutput,
                             V3ShortlistItem, V3ShortlistOutput)
from app.v3_scout import build_scout_report, scout_distribution
from app.v3_selection import V3SelectionError, validate_final_selection, validate_shortlist


def opportunity(name: str = '株式会社ABC') -> Opportunity:
    event = Event(
        company_name=name, event_type='brand_refresh', title='採用サイトを刷新',
        summary='採用サイトを刷新した', published_at=date(2026, 9, 20),
        source_type='web_search', source_name='Web Search',
        source_url='https://abc.example/news/1', source_title='公式発表',
        evidence=[EventEvidence(source_type='web_search', source_name='Web Search',
                                source_url='https://abc.example/news/1', source_title='公式発表')],
        strength=70, company_website='https://abc.example', location='神奈川県',
        possible_video_need='採用と技術を伝える映像')
    return Opportunity(company_name=name, events=[event], candidate=event.to_candidate(),
                       origins=['web_search'], sources=[])


def test_v3_settings_are_opt_in_and_configurable():
    settings = Settings(sales_pipeline_version='v3', v3_shortlist_size=15,
                        v3_final_size=5, v3_research_reasoning_effort='xhigh')
    assert settings.sales_pipeline_version == 'v3'
    assert settings.v3_shortlist_model == 'gpt-5.6-terra'
    assert settings.v3_research_model == 'gpt-5.6-luna'
    with pytest.raises(ValueError):
        Settings(sales_pipeline_version='v4')


def test_scout_report_is_observed_first_and_keeps_discovery_origin():
    text, records = build_scout_report([opportunity()])
    assert len(records) == 1
    assert records[0]['discovery_source'] == 'Web Search'
    assert 'Found via' in text
    assert 'Hypothesis to test' in text
    assert 'Budget unknown' not in text
    assert scout_distribution(records) == {'Recruitment / Corporate change': 1}


def test_v3_selection_rejects_duplicate_and_unknown_ids():
    valid = V3ShortlistOutput(selected=[V3ShortlistItem(
        company_id='a', company_name='A', why_selected='理由')])
    validate_shortlist(valid, {'a', 'b'}, 15)
    duplicate = V3ShortlistOutput(selected=[
        V3ShortlistItem(company_id='a', company_name='A', why_selected='理由'),
        V3ShortlistItem(company_id='a', company_name='A', why_selected='重複'),
    ])
    with pytest.raises(V3SelectionError):
        validate_shortlist(duplicate, {'a'}, 15)
    final = V3FinalSelectorOutput(selected=[V3FinalSelectionItem(
        rank=1, company_id='a', company_name='A', why_now='今',
        why_this_company='適合', proposed_angle='映像', main_risk='規模')])
    validate_final_selection(final, {'a'}, 5)
    with pytest.raises(V3SelectionError):
        validate_final_selection(final.model_copy(update={'selected': [
            final.selected[0].model_copy(update={'company_id': 'z'})]}), {'a'}, 5)


def test_v3_repository_tables_are_additive(tmp_path):
    factory = init_database(f'sqlite:///{tmp_path / "v3.db"}')
    with factory() as session:
        assert session.query(V3CandidatePool).count() == 0
        assert session.query(V3ShortlistDecision).count() == 0
        assert session.query(V3SalesMemo).count() == 0
        assert session.query(V3FinalSelection).count() == 0
    factory.kw['bind'].dispose()


class FakeResearchClient:
    def prompt(self, name):
        assert name == 'v3_research'
        return 'research prompt'

    async def generate(self, **kwargs):
        assert kwargs['model'] == 'gpt-5.6-luna'
        assert kwargs['reasoning_effort'] == 'xhigh'
        assert kwargs['use_web_search'] is True
        value = V3SalesMemoOutput(
            company_id='abc', company_name='株式会社ABC',
            why_this_company='地域企業で変化がある', why_now='採用サイト刷新',
            what_we_learned=['採用サイトが刷新された'],
            current_expression='サイトは文章と写真中心', expression_gap='medium',
            expression_gap_reason='仕事内容の動きが伝わりにくい',
            peer_comparison='同業は短い採用動画を掲載',
            creative_situation='固定関係は確認できない',
            opportunity_hypothesis='技術職向け短編採用映像',
            reasons_not_to_pursue=[], salesmans_take='営業したい', evidence=[
                V3Evidence(claim='公式発表', evidence_type='observed',
                           source_url='https://abc.example/news/1', confidence='high'),
                V3Evidence(claim='未検証の推測', evidence_type='inference',
                           source_url='https://untrusted.example/nope', confidence='low'),
            ])
        return Generation(value, {'https://abc.example/news/1'})


def test_v3_research_degrades_untrusted_evidence_without_failing():
    result = asyncio.run(run_v3_sales_research(
        FakeResearchClient(), Settings(), opportunity(),
        {'company_id': opportunity().opportunity_id, 'company_name': '株式会社ABC',
         'why_selected': '変化がある'}))
    assert len(result.value.evidence) == 2
    assert result.value.evidence[0].source_url == 'https://abc.example/news/1'
    assert result.value.evidence[1].source_url is None
    assert result.value.evidence[1].evidence_type == 'unknown'
    assert result.warnings == ['untrusted evidence URL removed']
