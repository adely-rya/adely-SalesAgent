"""Gate, research, and score company-level Opportunities."""
from __future__ import annotations

import logging

from app.config import Settings
from app.deduplication import canonical_url
from app.domain import Opportunity
from app.gating import evaluate_cheap_win, gate_status, hard_filter
from app.llm import LLMClient
from app.scoring import evaluation_priority, score_company
from app.vc_profiles import select_profile_context
from app.diagnostics import diagnostic_context, run_diagnostic_research

log = logging.getLogger(__name__)


async def gate_opportunities(opportunities: list[Opportunity], client: LLMClient,
                             settings: Settings, vc_profiles: list[dict],
                             errors: list[tuple[str, str, str]]) -> list[Opportunity]:
    """Run the obvious eligibility check and local-data-only Cheap WIN gate."""
    for opportunity in opportunities:
        deterministic_check = hard_filter(opportunity)
        if deterministic_check.excluded:
            opportunity.status = 'drop'
            opportunity.gate = {'status': 'drop', 'hard_filter': deterministic_check.model_dump(),
                                'reason': deterministic_check.reason}
            continue
        source_names = {source['provider'] for source in opportunity.sources if source['type'] == 'vc_news'}
        matching_profiles = select_profile_context(vc_profiles, source_names)
        try:
            win_result = await evaluate_cheap_win(client, settings, opportunity, matching_profiles, deterministic_check)
            win_data = win_result.value.model_dump()
            status = gate_status(win_result.value, settings)
            opportunity.status = status
            opportunity.gate = {'status': status, 'hard_filter': deterministic_check.model_dump(),
                                'win_pre': win_data, 'vc_profile_context': matching_profiles,
                                'reason': win_data['reason']}
        except Exception as exc:
            opportunity.status = 'hold'
            opportunity.gate = {'status': 'hold', 'hard_filter': deterministic_check.model_dump(),
                                'reason': f'Cheap WIN could not complete: {type(exc).__name__}'}
            errors.append(('cheap_win', opportunity.company_name, type(exc).__name__))
            log.warning('opportunity gate held company=%s type=%s', opportunity.company_name, type(exc).__name__)
    return opportunities


async def research_opportunities(opportunities: list[Opportunity], client: LLMClient,
                                 settings: Settings, vc_profiles: list[dict],
                                 errors: list[tuple[str, str, str]]) -> list[Opportunity]:
    """Deep research only eligible Opportunities; unknowns remain explicit in the output schema."""
    for opportunity in opportunities:
        eligible = opportunity.status == 'research' or (opportunity.status == 'hold' and settings.diagnostic_include_hold)
        if not eligible:
            continue
        source_names = {source['provider'] for source in opportunity.sources if source['type'] == 'vc_news'}
        matching_profiles = select_profile_context(vc_profiles, source_names)
        try:
            result = await run_diagnostic_research(client, settings, opportunity,
                opportunity.gate['win_pre'] if opportunity.gate else {}, matching_profiles)
            opportunity.research = result.value
            opportunity.research_evidence_urls = sorted(result.evidence_urls)
            opportunity.status = 'researched'
        except Exception as exc:
            opportunity.status = 'hold'
            errors.append(('diagnostic', opportunity.company_name, type(exc).__name__))
            log.warning('opportunity research held company=%s type=%s', opportunity.company_name, type(exc).__name__)
    return opportunities


async def score_opportunities(opportunities: list[Opportunity], client: LLMClient,
                              settings: Settings,
                              errors: list[tuple[str, str, str]]) -> list[Opportunity]:
    """Score researched Opportunities using the existing NEED / WIN / DELIVER rubric."""
    for opportunity in opportunities:
        if opportunity.status != 'researched' or opportunity.research is None:
            continue
        score_context = {
            'discovery_origins': opportunity.origins,
            'discovery_sources': opportunity.sources,
            'events': [event.model_dump(mode='json') for event in opportunity.events],
            'gate': opportunity.gate,
            'research': diagnostic_context(opportunity.research),
            'research_evidence_urls': opportunity.research_evidence_urls,
        }
        try:
            result = await score_company(client, settings, opportunity.candidate, score_context)
            trusted_urls = {canonical_url(str(event.source_url)) for event in opportunity.events}
            trusted_urls.update(canonical_url(str(event.company_website)) for event in opportunity.events
                                if event.company_website)
            trusted_urls.update(canonical_url(str(evidence.source_url))
                                for event in opportunity.events for evidence in event.evidence)
            trusted_urls.update(canonical_url(str(fact.source_url))
                                for event in opportunity.events for fact in event.research_facts)
            trusted_urls.update(canonical_url(url) for url in opportunity.research_evidence_urls)
            for profile in (opportunity.gate or {}).get('vc_profile_context', []):
                trusted_urls.update(canonical_url(str(url)) for url in profile.get('evidence', []) if url)
            if any(canonical_url(str(url)) not in trusted_urls
                   for risk in result.risks for url in risk.source_urls):
                raise ValueError('Score risk referenced a URL not present in Opportunity evidence')
            opportunity.score = result
            opportunity.final_score = evaluation_priority(result)
            opportunity.status = 'scored'
        except Exception as exc:
            opportunity.status = 'hold'
            errors.append(('scoring', opportunity.company_name, type(exc).__name__))
            log.warning('opportunity scoring held company=%s type=%s', opportunity.company_name, type(exc).__name__)
    return opportunities
