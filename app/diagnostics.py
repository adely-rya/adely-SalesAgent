"""Expensive, structured research used only after the cheap WIN gate passes."""
from __future__ import annotations

import json

from app.config import Settings
from app.deduplication import canonical_url
from app.llm import Generation, LLMClient
from app.schemas import DiagnosticOutput
from app.domain import Opportunity


async def run_diagnostic_research(client: LLMClient, settings: Settings, candidate: Opportunity,
                                  win_pre: dict, vc_profiles: list[dict]) -> Generation[DiagnosticOutput]:
    """Research current expression, debt, optional peer gap, and creative lock-in in one bounded call."""
    result = await client.generate(model=settings.diagnostic_model, instructions=client.prompt('diagnostic'),
        input_text=json.dumps({
            'candidate': candidate.model_dump(), 'win_pre': win_pre, 'vc_profiles': vc_profiles,
            'peer_research_enabled': settings.peer_research_enabled,
        }, ensure_ascii=False), output_type=DiagnosticOutput, use_web_search=settings.diagnostic_web_search,
        reasoning_effort=settings.diagnostic_reasoning_effort)
    trusted_urls = {canonical_url(url) for url in result.evidence_urls}
    trusted_urls.update(canonical_url(str(event.source_url)) for event in candidate.events)
    trusted_urls.update(canonical_url(str(event.company_website)) for event in candidate.events
                        if event.company_website)
    trusted_urls.update(canonical_url(str(evidence.source_url))
                        for event in candidate.events for evidence in event.evidence)
    trusted_urls.update(canonical_url(str(fact.source_url))
                        for event in candidate.events for fact in event.research_facts)
    for profile in vc_profiles:
        trusted_urls.update(canonical_url(str(url)) for url in profile.get('evidence', []) if url)

    referenced_urls = [str(evidence.source_url) for evidence in result.value.evidence
                       if evidence.source_url is not None]
    referenced_urls.extend(str(asset.url) for asset in result.value.current_expression.assets if asset.url)
    if result.value.peer_gap is not None:
        referenced_urls.extend(reference.source_url for reference in result.value.peer_gap.peers
                               if reference.source_url)
    if any(canonical_url(url) not in trusted_urls for url in referenced_urls):
        raise ValueError('Diagnostic output referenced a URL not present in tool or input evidence')
    return result


def diagnostic_context(value: DiagnosticOutput) -> dict:
    """The bounded research payload passed forward to Scoring and Trace."""
    return {
        'current_expression': value.current_expression.model_dump(mode='json'),
        'expression_debt': value.expression_debt.model_dump(mode='json'),
        'peer_gap': value.peer_gap.model_dump(mode='json') if value.peer_gap else None,
        'creative_lock_in': value.creative_lock_in.model_dump(mode='json'),
        'evidence': [item.model_dump(mode='json') for item in value.evidence],
    }
