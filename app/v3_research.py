"""One-company Deep Sales Research for V3."""
from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.deduplication import canonical_url
from app.domain import Opportunity
from app.llm import Generation, LLMClient
from app.v3_schemas import V3SalesMemoOutput


def _trusted_urls(opportunity: Opportunity, evidence_urls: set[str]) -> set[str]:
    values = set(evidence_urls)
    for event in opportunity.events:
        values.add(str(event.source_url))
        values.update(str(item.source_url) for item in event.evidence)
        values.update(str(fact.source_url) for fact in event.research_facts)
    if opportunity.candidate.website:
        values.add(str(opportunity.candidate.website))
    trusted: set[str] = set()
    for value in values:
        try:
            trusted.add(canonical_url(value))
        except (TypeError, ValueError):
            continue
    return trusted


def _sanitize_memo(generation: Generation[V3SalesMemoOutput],
                   opportunity: Opportunity) -> Generation[V3SalesMemoOutput]:
    trusted = _trusted_urls(opportunity, generation.evidence_urls)
    warnings: list[str] = list(generation.warnings)
    evidence = []
    for item in generation.value.evidence:
        if not item.source_url:
            evidence.append(item)
            continue
        try:
            valid = canonical_url(item.source_url) in trusted
        except (TypeError, ValueError):
            valid = False
        if valid:
            evidence.append(item)
        else:
            warnings.append('untrusted evidence URL removed')
            evidence.append(item.model_copy(update={
                'source_url': None, 'evidence_type': 'unknown',
                'confidence': 'low'}))
    memo = generation.value.model_copy(update={'evidence': evidence})
    return Generation(memo, set(generation.evidence_urls), list(dict.fromkeys(warnings)))


async def run_v3_sales_research(client: LLMClient, settings: Settings,
                                opportunity: Opportunity,
                                shortlist_item: dict[str, Any],
                                *, candidate_ref: str | None = None,
                                raw_record: dict[str, Any] | None = None
                                ) -> Generation[V3SalesMemoOutput]:
    explicit_ref = candidate_ref or shortlist_item.get('candidate_ref')
    ref = explicit_ref
    if not ref:
        # Backward-compatible direct callers may still provide company_id,
        # but that value is converted before entering the model payload.
        ref = shortlist_item.get('company_id')
    public_record = dict(raw_record or {})
    public_record.pop('company_id', None)
    public_record.pop('_company_id', None)
    payload = {
        'candidate_ref': ref,
        'company': public_record,
        'opportunity': {
            'candidate_ref': ref,
            'company_name': opportunity.company_name,
            'events': [event.model_dump(mode='json') for event in opportunity.events],
            'discovery_source': opportunity.origins,
        },
        'research_priority_reason': shortlist_item.get('reason',
                                                       shortlist_item.get('why_selected', '')),
    }
    generation = await client.generate(
        model=settings.v3_research_model,
        instructions=client.prompt('v3_research'),
        input_text=json.dumps(payload, ensure_ascii=False),
        output_type=V3SalesMemoOutput,
        use_web_search=settings.v3_research_web_search,
        reasoning_effort=settings.v3_research_reasoning_effort)
    result = _sanitize_memo(generation, opportunity)
    if explicit_ref and result.value.candidate_ref != ref:
        raise ValueError('research output candidate_ref does not match requested candidate')
    return result
