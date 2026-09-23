import asyncio
import json

import pytest

from app.config import Settings
from app.domain import Opportunity
from app.gating import (BatchGateValidationError, evaluate_cheap_win_batch,
                        gate_decision, hard_filter)
from app.llm import Generation
from app.opportunity_stages import gate_opportunities
from app.schemas import (Candidate, CheapWinBatchOutput, CheapWinBatchResult,
                         CheapWinOutput, Event)


def opportunity(index: int) -> Opportunity:
    company = f'Local Company {index}'
    event = Event(company_name=company, event_type='rebrand', title='VI刷新',
                  summary='ブランドメッセージを刷新', source_type='web_search',
                  source_name='Web Search', source_url=f'https://example.com/{index}',
                  source_title='公式発表', strength=80)
    candidate = Candidate(company_name=company, website=f'https://example.com/{index}',
        trigger_type='rebrand', trigger_title='VI刷新', trigger_summary='ブランドメッセージを刷新',
        published_at=None, source_url=f'https://example.com/{index}', source_title='公式発表',
        location='神奈川県', possible_video_need='ブランド映像')
    return Opportunity(company_name=company, events=[event], candidate=candidate,
                       origins=['web_search'])


class BatchClient:
    def __init__(self, *, fail_call=None, mode='research'):
        self.calls = []
        self.fail_call = fail_call
        self.mode = mode

    def prompt(self, name):
        return name

    async def generate(self, *, output_type, input_text, **kwargs):
        assert output_type is CheapWinBatchOutput
        payload = json.loads(input_text)
        self.calls.append((payload, kwargs))
        if self.fail_call == len(self.calls):
            raise RuntimeError('isolated batch failure')
        results = []
        for item in payload['companies']:
            research = self.mode == 'research'
            results.append(CheapWinBatchResult(
                company_id=item['company_id'], company_name=item['company_name'],
                win_pre=4.0, confidence='low', hard_blocker=False,
                risk_tags=[], reason='Observed rebrand and a concrete brand-film hypothesis',
                decision='RESEARCH' if research else 'HOLD',
                trigger_quality='strong' if research else 'weak',
                target_fit='good', research_value='high' if research else 'low'))
        return Generation(CheapWinBatchOutput(results=results), set())


def test_21_opportunities_use_five_batches_and_high_reasoning():
    items = [opportunity(index) for index in range(21)]
    client = BatchClient()
    errors = []
    settings = Settings(gate_batch_size=5, cheap_win_reasoning_effort='high')
    asyncio.run(gate_opportunities(items, client, settings, [], errors))
    assert [len(call[0]['companies']) for call in client.calls] == [5, 5, 5, 5, 1]
    assert len(client.calls) == 5
    assert all(call[1]['reasoning_effort'] == 'high' for call in client.calls)
    assert all(item.status == 'research' for item in items)
    assert errors == []


def test_batch_failure_is_isolated_to_one_batch():
    items = [opportunity(index) for index in range(11)]
    client = BatchClient(fail_call=2)
    errors = []
    asyncio.run(gate_opportunities(items, client, Settings(), [], errors))
    assert len(client.calls) == 3
    assert [item.status for item in items] == ['research'] * 5 + ['hold'] * 5 + ['research']
    assert len(errors) == 5


@pytest.mark.parametrize('bad', ['missing', 'duplicate', 'unknown'])
def test_batch_result_company_id_validation(bad):
    item = opportunity(1)

    class InvalidClient(BatchClient):
        async def generate(self, *, output_type, input_text, **kwargs):
            generation = await super().generate(output_type=output_type, input_text=input_text, **kwargs)
            result = generation.value.results[0]
            if bad == 'missing':
                generation.value.results = []
            elif bad == 'duplicate':
                generation.value.results.append(result.model_copy())
            else:
                result.company_id = 'unknown-id'
            return generation

    with pytest.raises(BatchGateValidationError):
        asyncio.run(evaluate_cheap_win_batch(
            InvalidClient(), Settings(), [(item, hard_filter(item))], []))


def test_low_confidence_unknown_and_weak_trigger_do_not_route_to_research():
    base = dict(win_pre=4, confidence='low', hard_blocker=False, risk_tags=[],
                unknown_factors=['購買経路不明'], reason='Unknown only', decision='RESEARCH',
                trigger_quality='weak', target_fit='good', research_value='high')
    assert gate_decision(CheapWinOutput(**base), Settings(), opportunity(1))[0] == 'hold'
    strong = CheapWinOutput(**{**base, 'trigger_quality': 'strong'})
    assert gate_decision(strong, Settings(), opportunity(1))[0] == 'research'


def test_listed_parent_is_hold_and_no_research_quota_is_imposed():
    listed = CheapWinOutput(win_pre=9, confidence='high', hard_blocker=False,
        listed_company=True, risk_tags=[], reason='listed', decision='RESEARCH',
        trigger_quality='strong', target_fit='good', research_value='high')
    assert gate_decision(listed, Settings(), opportunity(1))[0] == 'hold'
    items = [CheapWinOutput(win_pre=4, confidence='medium', hard_blocker=False,
        risk_tags=[], reason='strong', decision='RESEARCH', trigger_quality='strong',
        target_fit='good', research_value='high') for _ in range(5)]
    assert [gate_decision(item, Settings(), opportunity(i))[0] for i, item in enumerate(items)] == ['research'] * 5
