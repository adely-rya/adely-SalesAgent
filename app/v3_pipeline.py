"""V3 pipeline: recall-heavy discovery, budget allocation, research, then selection."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
import json
import logging
import time
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from app.collectors import CollectionResult, collect_fixed_sources
from app.config import Settings
from app.database import init_database
from app.diagnostics import safe_exception_message, safe_exception_traceback
from app.domain import Event, Opportunity, RawItem
from app.llm import LLMClient
from app.prefilters import PrefilterDecision, log_prefilter_metrics
from app.report import format_error_report, send_discord_report
from app.v2_discovery import (WebDiscoveryFailure, discover_web_events,
                              extract_fixed_events, group_events_by_company,
                              merge_events)
from app.v2_repository import V2Repository
from app.v3_research import run_v3_sales_research
from app.v3_report import send_v3_report
from app.v3_repository import V3Repository
from app.v3_schemas import V3FinalSelectorOutput, V3RankingOutput
from app.v3_scout import build_scout_report, scout_distribution
from app.v3_selection import (deterministic_research_refs, research_allocation_mode,
                              validate_final_selection, validate_ranking)

log = logging.getLogger(__name__)

BUSINESS_CONTEXT = """
adely is a small video production studio serving primarily Kanagawa, then Tokyo and the wider
capital region. It can deliver live action, motion graphics, 3DCG, compositing, brand films,
corporate films, recruiting films, service explainers and short-form derivatives. The practical
starting range is about 200,000-500,000 JPY. The strongest production shape is a 30-90 second
web, recruiting, sales, service or brand film with a small number of locations.
Prefer credible sales opportunities at private SMEs and mid-market/local companies, but do not
invent facts or impose a hard employee-count cutoff. A new creative partner may be useful when
the company has a real communication change and an attainable production need.
""".strip()


def _evaluated_prefilter_events(raw_items: list[RawItem],
                                decisions: dict[int, PrefilterDecision]):
    from app.prefilters import EvaluatedEvent
    return [EvaluatedEvent(item.id, item.source_type, item.source_name, item.title,
                           decisions[item.id]) for item in raw_items if item.id in decisions]


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    """Remove internal identifiers before a record crosses the LLM boundary."""
    return {key: value for key, value in record.items()
            if key not in {'company_id', '_company_id'}}


def _bypass_selection(records: list[dict]) -> list[dict]:
    return [{'candidate_ref': record['candidate_ref'],
             'reason': 'Candidate pool is within the daily research budget; investigate this candidate.'}
            for record in records]


async def _rank_for_research(client: LLMClient, settings: Settings, raw_report: str,
                             records: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    """Rank every candidate; a single correction retry is allowed by the client."""
    refs = {record['candidate_ref'] for record in records}
    payload = {
        'business_context': BUSINESS_CONTEXT,
        'candidate_count': len(records),
        'candidate_pool': [_public_record(record) for record in records],
        'raw_daily_sales_report': raw_report,
    }
    generation = await client.generate(
        model=settings.v3_shortlist_model,
        instructions=client.prompt('v3_shortlist'),
        input_text=json.dumps(payload, ensure_ascii=False),
        output_type=V3RankingOutput, use_web_search=False,
        reasoning_effort=settings.v3_shortlist_reasoning_effort,
        max_validation_retries=1)
    validate_ranking(generation.value, refs)
    ranking = [item.model_dump(mode='json') for item in generation.value.ranking]
    return ranking[:settings.v3_shortlist_size], {
        'ranking': ranking,
        'model': settings.v3_shortlist_model,
        'reasoning_effort': settings.v3_shortlist_reasoning_effort,
        'prompt_version': settings.v3_shortlist_prompt_version,
        'api_calls': 1,
    }


async def _research_selected(client: LLMClient, settings: Settings,
                             opportunities_by_ref: dict[str, Opportunity],
                             records_by_ref: dict[str, dict], selected: list[dict],
                             errors: list[tuple[str, str, str]]) -> dict[str, dict[str, Any]]:
    memos: dict[str, dict[str, Any]] = {}
    for allocation in selected:
        ref = allocation['candidate_ref']
        opportunity = opportunities_by_ref.get(ref)
        if opportunity is None:
            errors.append(('research', ref, 'UnknownCandidateRef'))
            continue
        try:
            generation = await run_v3_sales_research(
                client, settings, opportunity, allocation,
                candidate_ref=ref, raw_record=records_by_ref.get(ref))
            memo = generation.value.model_dump(mode='json')
            memo['_company_id'] = opportunity.opportunity_id
            memo['_evidence_urls'] = sorted(generation.evidence_urls)
            memo['_warnings'] = generation.warnings
            memos[ref] = memo
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(('research', opportunity.company_name, type(exc).__name__))
            log.warning('v3 research failed ref=%s company=%s type=%s message=%s',
                        ref, opportunity.company_name, type(exc).__name__,
                        safe_exception_message(exc))
    return memos


async def _final_select(client: LLMClient, settings: Settings,
                        memos_by_ref: dict[str, dict[str, Any]]) -> list[dict]:
    if not memos_by_ref:
        return []
    memo_payload = [{key: value for key, value in memo.items()
                     if not key.startswith('_')}
                    for memo in memos_by_ref.values()]
    generation = await client.generate(
        model=settings.v3_final_selector_model,
        instructions=client.prompt('v3_final_selector'),
        input_text=json.dumps({'business_context': BUSINESS_CONTEXT,
                               'sales_memos': memo_payload}, ensure_ascii=False),
        output_type=V3FinalSelectorOutput, use_web_search=False,
        reasoning_effort=settings.v3_final_selector_reasoning_effort)
    expected = min(settings.v3_final_size, len(memos_by_ref))
    validate_final_selection(generation.value, set(memos_by_ref), settings.v3_final_size,
                             exact_count=expected)
    return [item.model_dump(mode='json') for item in generation.value.selected]


async def run_v3_pipeline(settings: Settings, client: LLMClient | None = None, *,
                          collect: bool = False, web_discovery: bool = True,
                          fixed_discovery: bool = True, topics: list[str] | None = None,
                          collector: Callable[..., Awaitable[CollectionResult]] = collect_fixed_sources
                          ) -> bool:
    factory = init_database(settings.database_url)
    repository = V3Repository(factory)
    run = repository.start_run(settings, collect=collect, web_discovery=web_discovery,
                               fixed_discovery=fixed_discovery, topics=topics)
    owned_client = client is None
    client = client or LLMClient(settings)
    started = time.monotonic()
    errors: list[tuple[str, str, str]] = []
    error_labels: list[str] = []
    raw_items: list[RawItem] = []
    prefilter_decisions: dict[int, PrefilterDecision] = {}
    try:
        if not settings.has_api_key:
            raise ValueError('Missing API key')
        if collect:
            collection = await collector(settings, factory)
            errors.extend(('collector', message, 'CollectionError')
                          for message in collection.errors)
            error_labels.extend(f'collector: {message}' for message in collection.errors)

        input_repository = V2Repository(factory)
        raw_items, _vc_profiles = input_repository.load_pipeline_inputs(
            settings.source_prefilter_batch_size)
        fixed_events: list[Event] = []
        if fixed_discovery:
            fixed_events, prefilter_decisions, _processed, rejected = await extract_fixed_events(
                client, settings, raw_items,
                include_hold=settings.fixed_discovery_include_hold_events)
            log_prefilter_metrics(_evaluated_prefilter_events(raw_items, prefilter_decisions))
            if rejected:
                error_labels.append(f'fixed discovery rejected {rejected} event(s)')

        web_events: list[Event] = []
        if web_discovery:
            failures: list[WebDiscoveryFailure] = []
            web_events, _rejections = await discover_web_events(
                client, settings, topics, failures=failures)
            for failure in failures:
                errors.append(('discovery', f'web topic: {failure.topic}', failure.error_type))
                error_labels.append(f'discovery: web topic: {failure.topic} ({failure.error_type})')

        opportunities = group_events_by_company(merge_events(fixed_events, web_events))
        raw_report, records = build_scout_report(opportunities)
        records_by_ref = {record['candidate_ref']: record for record in records}
        opportunities_by_ref = {
            record['candidate_ref']: opportunity
            for record, opportunity in zip(records, opportunities)
        }
        mapping = {record['candidate_ref']: record['company_id'] for record in records}
        repository.save_candidate_pool(run.id, records, raw_report, mapping)

        selected: list[dict] = []
        allocation_mode = research_allocation_mode(len(records), settings.v3_shortlist_size)
        ranking_trace: dict[str, Any] = {}
        shortlist_api_calls = 0
        if records:
            if allocation_mode == 'bypass':
                selected = _bypass_selection(records)
                ranking_trace = {'ranking': [], 'reason': 'candidate_count <= research budget'}
            else:
                allocation_mode = 'terra_ranked'
                try:
                    selected, ranking_trace = await _rank_for_research(
                        client, settings, raw_report, records)
                    shortlist_api_calls = ranking_trace.get('api_calls', 1)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    allocation_mode = 'fallback'
                    fallback_refs = set(deterministic_research_refs(
                        records, settings.v3_shortlist_size))
                    selected = [item for item in _bypass_selection(records)
                                if item['candidate_ref'] in fallback_refs]
                    ranking_trace = {
                        'ranking': [], 'fallback': 'deterministic candidate order',
                        'error_type': type(exc).__name__,
                    }
                    error_labels.append(f'research allocation fallback ({type(exc).__name__})')
                    log.warning('v3 ranking fallback type=%s message=%s',
                                type(exc).__name__, safe_exception_message(exc))
            repository.save_shortlist(run.id, records, selected, settings,
                                      allocation_mode=allocation_mode)
        else:
            repository.save_shortlist(run.id, records, [], settings,
                                      allocation_mode=allocation_mode)

        research_web_before = getattr(client, 'web_search_request_count', None)
        memos = await _research_selected(client, settings, opportunities_by_ref,
                                         records_by_ref, selected, errors)
        research_web_after = getattr(client, 'web_search_request_count', None)
        for ref, memo in memos.items():
            repository.save_memo(run.id, mapping[ref], memo,
                                 memo.get('_evidence_urls', []),
                                 memo.get('_warnings', []), settings)

        final_selected: list[dict] = []
        if memos:
            try:
                final_selected = await _final_select(client, settings, memos)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(('final_selector', 'sales memos', type(exc).__name__))
                error_labels.append(f'final selector: sales memos ({type(exc).__name__})')
        repository.save_final(run.id, final_selected, mapping, settings)

        for stage, subject, error_type in errors:
            label = f'{stage}: {subject} ({error_type})'
            if label not in error_labels:
                error_labels.append(label)

        selected_for_report = []
        for selection in final_selected:
            ref = selection['candidate_ref']
            memo = memos.get(ref)
            opportunity = opportunities_by_ref.get(ref)
            if memo and opportunity:
                selected_for_report.append({'selection': selection,
                                            'memo': memo, 'opportunity': opportunity})
        warnings = sum(len(memo.get('_warnings', [])) for memo in memos.values())
        origin_counts = Counter(
            'combined' if len(set(opportunity.origins)) > 1
            else 'web' if 'web_search' in opportunity.origins else 'fixed'
            for opportunity in opportunities)
        summary = {
            'status': 'Partial' if errors else 'Completed',
            'scout_candidates': len(opportunities),
            'research_allocation_mode': allocation_mode,
            'shortlist_api_calls': shortlist_api_calls,
            'shortlisted': len(selected),
            'research_attempted': len(selected),
            'research_success': len(memos),
            'research_failed': len(selected) - len(memos),
            'final_selected': len(final_selected),
            'origins': {key: origin_counts.get(key, 0)
                        for key in ('fixed', 'web', 'combined')},
            'distribution': scout_distribution(records),
            'validation_warnings': warnings,
            'runtime_seconds': round(time.monotonic() - started, 2),
            'models': {
                'scout': settings.discovery_model,
                'ranking': 'bypass' if allocation_mode == 'bypass' else settings.v3_shortlist_model,
                'research': f'{settings.v3_research_model} {settings.v3_research_reasoning_effort}',
                'final': f'{settings.v3_final_selector_model} {settings.v3_final_selector_reasoning_effort}',
            },
            'api_plan': {
                'shortlist': shortlist_api_calls,
                'deep_research': len(selected),
                'deep_research_success': len(memos),
                'final_selector': 1 if memos else 0,
                'web_search_calls_total': getattr(client, 'web_search_request_count', None),
                'web_search_calls_deep_research': (
                    research_web_after - research_web_before
                    if research_web_before is not None and research_web_after is not None else None),
                'new_web_search_stages': ['deep_research'],
            },
            'candidate_ref_mapping': mapping,
            'research_targets': [item['candidate_ref'] for item in selected],
            'ranking_trace': ranking_trace,
            'final_selector_input_refs': list(memos),
            'discord': {'summary_and_top_selection_messages': len(selected_for_report) + 1},
        }
        run = repository.finish(run.id, settings=settings,
                                candidate_count=len(opportunities), shortlisted=len(selected),
                                research_success=len(memos), final_count=len(final_selected),
                                errors=errors, summary=summary)
        day = str(datetime.now(ZoneInfo(settings.timezone)).date())
        await send_v3_report(day, summary, selected_for_report, run.id,
                             settings.discord_webhook_url.get_secret_value())
        if error_labels:
            await send_discord_report(format_error_report(run.id, run.status, error_labels),
                                      settings.discord_webhook_url.get_secret_value())
        log.info('v3 run complete run_id=%s candidates=%d allocated=%d research=%d final=%d',
                 run.id, len(opportunities), len(selected), len(memos), len(final_selected))
        return run.status == 'completed'
    except asyncio.CancelledError:
        repository.mark_failed(run.id, 'CancelledError')
        raise
    except Exception as exc:
        error_type = type(exc).__name__
        log.error('v3 pipeline failed run_id=%s type=%s message=%s stack=%s',
                  run.id, error_type, safe_exception_message(exc),
                  safe_exception_traceback(exc))
        repository.mark_failed(run.id, error_type)
        await send_discord_report(format_error_report(run.id, 'failed',
                                                      [f'v3_pipeline: run ({error_type})']),
                                  settings.discord_webhook_url.get_secret_value())
        return False
    finally:
        if owned_client:
            await client.close()
        factory.kw['bind'].dispose()
