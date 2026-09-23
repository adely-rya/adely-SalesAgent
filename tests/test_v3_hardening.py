import asyncio
import json
import time
from types import SimpleNamespace

from app.config import Settings
from app.domain import Opportunity
from app.llm import Generation, LLMClient
from app.v3_pipeline import _research_selected
from app.v3_research import normalize_sales_memo_json
from app.v3_report import format_v3_messages
from app.v3_schemas import V3SalesMemoOutput


def _opportunity(ref: str) -> Opportunity:
    from tests.test_v3 import opportunity
    item = opportunity(f'Company {ref}')
    return item


def _memo(ref: str) -> V3SalesMemoOutput:
    return V3SalesMemoOutput(candidate_ref=ref, company_name=f'Company {ref}',
                             why_now='recent change', what_we_learned=['fact'],
                             opportunity_hypothesis='short film',
                             salesmans_take='contact')


def test_research_is_bounded_isolated_and_ordered(monkeypatch):
    active = 0
    maximum = 0
    completion_order = []

    async def fake_research(client, settings, opportunity, allocation, *, candidate_ref,
                            raw_record=None):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02 if candidate_ref == 'C001' else 0.001)
        completion_order.append(candidate_ref)
        active -= 1
        if candidate_ref == 'C002':
            raise ValueError('fixture failure')
        return Generation(_memo(candidate_ref), set())

    monkeypatch.setattr('app.v3_pipeline.run_v3_sales_research', fake_research)
    refs = [f'C{index:03d}' for index in range(1, 6)]
    opportunities = {ref: _opportunity(ref) for ref in refs}
    records = {ref: {'candidate_ref': ref, 'company_name': f'Company {ref}'} for ref in refs}
    errors = []
    details = []
    memos = asyncio.run(_research_selected(
        object(), Settings(v3_research_concurrency=3), opportunities, records,
        [{'candidate_ref': ref, 'reason': 'priority'} for ref in refs], errors, details))

    assert maximum <= 3
    assert completion_order != refs  # completion order is not the persistence order
    assert list(memos) == ['C001', 'C003', 'C004', 'C005']
    assert errors == [('research', 'Company C002', 'ValueError')]
    assert details[0]['candidate_ref'] == 'C002'


def test_research_concurrency_sweep_is_offline(monkeypatch):
    """Compare safe concurrency values without making any provider calls."""
    refs = [f'C{index:03d}' for index in range(1, 16)]
    opportunities = {ref: _opportunity(ref) for ref in refs}
    records = {ref: {'candidate_ref': ref, 'company_name': f'Company {ref}'} for ref in refs}
    allocations = [{'candidate_ref': ref, 'reason': 'priority'} for ref in refs]
    timings = {}

    for concurrency in (1, 2, 3, 4, 5, 10):
        active = 0
        maximum = 0

        async def fake_research(client, settings, opportunity, allocation, *, candidate_ref,
                                raw_record=None):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1
            return Generation(_memo(candidate_ref), set())

        monkeypatch.setattr('app.v3_pipeline.run_v3_sales_research', fake_research)
        started = time.monotonic()
        memos = asyncio.run(_research_selected(
            object(), Settings(v3_research_concurrency=concurrency), opportunities, records,
            allocations, [], []))
        timings[concurrency] = round(time.monotonic() - started, 3)
        assert maximum == concurrency
        assert list(memos) == refs

    print(f'offline concurrency sweep seconds={timings}')


def test_research_memo_normalization_is_local_and_non_inventive():
    raw = json.dumps({
        'candidate_ref': 'C001', 'company_name': 'Company C001',
        'why_now': 'trigger', 'what_we_learned': 'one fact',
        'opportunity_hypothesis': 'short film', 'salesmans_take': 'contact',
        'peer_comparison': '   ', 'evidence': None,
    })
    value = V3SalesMemoOutput.model_validate_json(normalize_sales_memo_json(raw))
    assert value.what_we_learned == ['one fact']
    assert value.peer_comparison is None
    assert value.evidence == []


def test_v3_discord_brief_is_compact_and_hides_unknowns():
    messages = format_v3_messages('2026-09-24', {
        'status': 'Completed', 'scout_candidates': 15,
        'research_allocation_mode': 'bypass', 'shortlisted': 15,
        'research_success': 15, 'research_attempted': 15,
        'origins': {'fixed': 0, 'web': 15, 'combined': 0},
        'usage_by_stage': {}, 'api_plan': {'web_search_calls_observed': 15},
    }, [{
        'selection': {'rank': 1, 'why_now': 'now', 'proposed_angle': 'angle',
                      'main_risk': 'risk'},
        'memo': {'company_name': 'Company C001', 'what_we_learned': ['a', 'b', 'c', 'd'],
                 'expression_gap': 'medium', 'expression_gap_reason': 'gap',
                 'peer_comparison': 'peer', 'opportunity_hypothesis': 'angle',
                 'reasons_not_to_pursue': ['watch', 'watch2', 'watch3'],
                 'evidence': [{'claim': 'source', 'source_url': 'https://example.com'}]},
        'opportunity': _opportunity('C001'),
    }], 99)
    assert len(messages) == 2
    assert '・d' not in messages[1]
    assert '・watch3' not in messages[1]
    assert 'Budget unknown' not in messages[1]
    assert messages[1].count('https://') <= 5


def test_llm_usage_is_recorded_by_stage_without_extra_requests():
    class SDK:
        class Responses:
            async def create(self, **kwargs):
                return SimpleNamespace(
                    status='completed',
                    output_text='{"video_need":8,"timing":8,"budget_fit":8,"adely_fit":8,"entry_chance":8,"location_fit":8,"reason":"ok","risks":[]}',
                    model_dump=lambda: {'output': [], 'usage': {
                        'input_tokens': 10, 'output_tokens': 7,
                        'output_tokens_details': {'reasoning_tokens': 3},
                        'input_tokens_details': {'cached_tokens': 2},
                    }})
        responses = Responses()

    from app.schemas import ScoreOutput
    client = LLMClient(Settings(), SDK())
    result = asyncio.run(client.generate(
        model='mock', instructions='mock', input_text='mock',
        output_type=ScoreOutput, stage='deep_research'))
    assert result.value.video_need == 8
    usage = client.observability_snapshot()['deep_research']
    assert usage['generate_calls'] == 1
    assert usage['api_requests'] == 1
    assert usage['input_tokens'] == 10
    assert usage['reasoning_tokens'] == 3
    assert usage['cached_input_tokens'] == 2
