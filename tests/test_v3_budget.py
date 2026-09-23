import asyncio
import json

import pytest

from app.config import Settings
from app.llm import Generation, InvalidOutputError
from app.v3_pipeline import _final_select, _rank_for_research
from app.v3_schemas import (V3FinalSelectionItem, V3FinalSelectorOutput,
                             V3RankingItem, V3RankingOutput)
from app.v3_selection import (V3SelectionError, deterministic_research_refs,
                              research_allocation_mode, validate_ranking)


@pytest.mark.parametrize(('count', 'mode', 'research_count', 'ranking_calls'), [
    (5, 'bypass', 5, 0), (12, 'bypass', 12, 0), (15, 'bypass', 15, 0),
    (16, 'terra_ranked', 15, 1), (30, 'terra_ranked', 15, 1),
])
def test_research_budget_allocation_contract(count, mode, research_count, ranking_calls):
    assert research_allocation_mode(count) == mode
    assert len(deterministic_research_refs(
        [{'candidate_ref': f'C{i:03d}'} for i in range(1, count + 1)])) == min(count, 15)
    assert ranking_calls == (1 if count > 15 else 0)


def test_candidate_refs_validate_unknown_duplicate_missing_and_format():
    known = {'C001', 'C002', 'C003'}
    validate_ranking(V3RankingOutput(ranking=[
        V3RankingItem(candidate_ref='C002', reason='b'),
        V3RankingItem(candidate_ref='C001', reason='a'),
        V3RankingItem(candidate_ref='C003', reason='c'),
    ]), known)
    with pytest.raises(V3SelectionError):
        validate_ranking(V3RankingOutput(ranking=[
            V3RankingItem(candidate_ref='C001', reason='a'),
            V3RankingItem(candidate_ref='C001', reason='duplicate'),
            V3RankingItem(candidate_ref='C003', reason='c'),
        ]), known)
    with pytest.raises(V3SelectionError):
        validate_ranking(V3RankingOutput(ranking=[
            V3RankingItem(candidate_ref='C001', reason='a'),
            V3RankingItem(candidate_ref='C002', reason='b'),
            V3RankingItem(candidate_ref='C999', reason='unknown'),
        ]), known)
    with pytest.raises(V3SelectionError):
        validate_ranking(V3RankingOutput(ranking=[
            V3RankingItem(candidate_ref='X01', reason='bad'),
            V3RankingItem(candidate_ref='C002', reason='b'),
            V3RankingItem(candidate_ref='C003', reason='c'),
        ]), known)


class RankingClient:
    def __init__(self):
        self.input_text = None
        self.kwargs = None

    def prompt(self, name):
        assert name == 'v3_shortlist'
        return 'ranking prompt'

    async def generate(self, **kwargs):
        self.kwargs = kwargs
        self.input_text = kwargs['input_text']
        refs = [f'C{i:03d}' for i in range(1, 17)]
        return Generation(V3RankingOutput(ranking=[
            V3RankingItem(candidate_ref=ref, reason='research priority') for ref in refs
        ]), set())


def test_ranking_uses_refs_only_and_returns_top_15():
    client = RankingClient()
    records = [{'candidate_ref': f'C{i:03d}', 'company_id': f'db-{i}',
                'company_name': f'Company {i}'} for i in range(1, 17)]
    selected, trace = asyncio.run(_rank_for_research(
        client, Settings(), '## [C001] Company 1', records))
    assert len(selected) == 15
    assert selected[0]['candidate_ref'] == 'C001'
    assert trace['ranking'][-1]['candidate_ref'] == 'C016'
    payload = json.loads(client.input_text)
    assert all('company_id' not in record for record in payload['candidate_pool'])
    assert 'db-1' not in client.input_text
    assert client.kwargs['model'] == 'gpt-5.6-terra'
    assert client.kwargs['reasoning_effort'] == 'high'
    assert client.kwargs['max_validation_retries'] == 1


def test_fallback_order_is_deterministic_and_does_not_use_total_score():
    records = [
        {'candidate_ref': 'C001', 'total_score': 999},
        {'candidate_ref': 'C002', 'total_score': 1},
        {'candidate_ref': 'C003', 'total_score': 500},
    ]
    assert deterministic_research_refs(records, 2) == ['C001', 'C002']


class FinalClient:
    def __init__(self):
        self.input_text = None
        self.kwargs = None

    def prompt(self, name):
        assert name == 'v3_final_selector'
        return 'final prompt'

    async def generate(self, **kwargs):
        self.kwargs = kwargs
        self.input_text = kwargs['input_text']
        return Generation(V3FinalSelectorOutput(selected=[
            V3FinalSelectionItem(rank=index, candidate_ref=f'C{index:03d}',
                                 company_name=f'Company {index}', why_now='trigger',
                                 why_this_company='fit', proposed_angle='film',
                                 main_risk='scope')
            for index in range(1, 6)
        ]), set())


def test_final_selector_receives_all_successful_memos_and_returns_exactly_five():
    client = FinalClient()
    memos = {f'C{index:03d}': {
        'candidate_ref': f'C{index:03d}', 'company_name': f'Company {index}',
        'why_now': 'trigger', 'why_this_company': 'fit',
        'what_we_learned': ['fact'], 'current_expression': 'site',
        'expression_gap': 'medium', 'expression_gap_reason': 'gap',
        'peer_comparison': 'peers', 'creative_situation': 'open',
        'opportunity_hypothesis': 'film', 'reasons_not_to_pursue': [],
        'salesmans_take': 'contact', 'evidence': [{'claim': 'fact'}],
        '_company_id': f'db-{index}',
    } for index in range(1, 8)}
    selected = asyncio.run(_final_select(client, Settings(), memos))
    assert len(selected) == 5
    assert [item['rank'] for item in selected] == [1, 2, 3, 4, 5]
    payload = json.loads(client.input_text)
    assert len(payload['sales_memos']) == 7
    assert all('_company_id' not in memo for memo in payload['sales_memos'])
    assert client.kwargs['model'] == 'gpt-5.6-terra'
    assert client.kwargs['reasoning_effort'] == 'high'
