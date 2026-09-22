"""Readable V2.2 daily pipeline; V1's run_pipeline remains independent."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
import logging
import time
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from app.collectors import CollectionResult, collect_fixed_sources
from app.config import Settings
from app.database import init_database
from app.diagnostics import safe_exception_message, safe_exception_traceback
from app.domain import Event, Opportunity, RawItem
from app.llm import LLMClient
from app.models import Run
from app.opportunity_stages import (gate_opportunities, research_opportunities,
                                    score_opportunities)
from app.prefilters import PrefilterDecision, log_prefilter_metrics
from app.report import (format_error_report, format_opportunity_report, format_warning_report,
                        send_discord_report)
from app.strategy import generate_strategy
from app.v2_discovery import (discover_web_events, extract_fixed_events,
                              WebDiscoveryFailure, group_events_by_company, merge_events)
from app.v2_repository import V2Repository

log = logging.getLogger(__name__)


async def run_daily_pipeline(settings: Settings, client: LLMClient | None = None, *,
                             collect: bool = False, web_discovery: bool = True,
                             fixed_discovery: bool = True, topics: list[str] | None = None,
                             collector: Callable[..., Awaitable[CollectionResult]] = collect_fixed_sources) -> bool:
    """Collect/load, transform Python objects through the opportunity stages, then save once."""
    factory = init_database(settings.database_url)
    repository = V2Repository(factory)
    run = repository.start_run(settings, collect=collect, web_discovery=web_discovery,
                               fixed_discovery=fixed_discovery, topics=topics)
    owned_client = client is None
    client = client or LLMClient(settings)
    errors: list[tuple[str, str, str]] = []
    error_labels: list[str] = []
    warning_labels: list[str] = []
    error_details: dict[tuple[str, str, str], dict[str, str]] = {}
    started_monotonic = time.monotonic()
    raw_items: list[RawItem] = []
    prefilter_decisions: dict[int, PrefilterDecision] = {}
    processed_raw_item_ids: set[int] = set()
    opportunities: list[Opportunity] = []
    try:
        if not settings.has_api_key:
            raise ValueError('Missing API key')

        if collect:
            collection = await collector(settings, factory)
            errors.extend(('collector', message, 'CollectionError') for message in collection.errors)
            error_labels.extend(f'collector: {message}' for message in collection.errors)

        raw_items, vc_profiles = repository.load_pipeline_inputs(settings.source_prefilter_batch_size)

        fixed_events: list[Event] = []
        if fixed_discovery:
            fixed_events, prefilter_decisions, processed_raw_item_ids, rejected = await extract_fixed_events(
                client, settings, raw_items, include_hold=settings.fixed_discovery_include_hold_events)
            log_prefilter_metrics(_evaluated_prefilter_events(raw_items, prefilter_decisions))
            log.info('fixed event extraction raw_items=%d events=%d rejected=%d consumed=%d',
                     len(raw_items), len(fixed_events), rejected, len(processed_raw_item_ids))
            if rejected:
                warning_labels.append(f'fixed_discovery: rejected={rejected}')

        web_events: list[Event] = []
        if web_discovery:
            web_failures: list[WebDiscoveryFailure] = []
            web_events, rejections = await discover_web_events(
                client, settings, topics, failures=web_failures)
            rejection_reasons = dict(sorted(Counter(item.reason for item in rejections).items()))
            log.info('web_discovery accepted=%d rejected=%d rejection_reasons=%s',
                     len(web_events), len(rejections), rejection_reasons)
            for failure in web_failures:
                errors.append(('discovery', f'web topic: {failure.topic}', failure.error_type))
            if web_failures:
                log.warning('web_discovery completed with failures=%d', len(web_failures))

        all_events = merge_events(fixed_events, web_events)
        opportunities = group_events_by_company(all_events)
        log.info('opportunity grouping events=%d companies=%d', len(all_events), len(opportunities))

        await gate_opportunities(opportunities, client, settings, vc_profiles, errors)
        for stage, subject, error_type in errors:
            if error_type not in {'CollectionError', 'RejectedEventOutput'}:
                error_labels.append(f'{stage}: {subject} ({error_type})')

        gate_counts = Counter(opportunity.status for opportunity in opportunities)
        research_attempted = sum(
            1 for opportunity in opportunities
            if opportunity.status == 'research'
            or (opportunity.status == 'hold' and settings.diagnostic_include_hold))
        await research_opportunities(opportunities, client, settings, vc_profiles, errors, error_details)
        research_success = sum(1 for opportunity in opportunities if opportunity.status == 'researched')
        for opportunity in opportunities:
            warning_labels.extend(
                f'research: {opportunity.company_name} ({warning})'
                for warning in opportunity.research_warnings)
        research_failure_counts = Counter(error_type for stage, _subject, error_type in errors
                                          if stage == 'diagnostic')
        await score_opportunities(opportunities, client, settings, errors)
        scoring_attempted = research_success
        scoring_success = sum(1 for opportunity in opportunities if opportunity.status == 'scored')
        for stage, subject, error_type in errors:
            label = f'{stage}: {subject} ({error_type})'
            if label not in error_labels:
                error_labels.append(label)

        top_opportunities = sorted((opportunity for opportunity in opportunities if opportunity.score is not None),
                                   key=lambda opportunity: (-(opportunity.final_score or 0), opportunity.company_name))[:settings.top_candidates]
        if settings.v2_generate_strategy:
            for opportunity in top_opportunities:
                try:
                    generated = await generate_strategy(client, settings, opportunity.candidate,
                        opportunity.final_score or 0, strategy_context={
                            'events': [event.model_dump(mode='json') for event in opportunity.events],
                            'gate': opportunity.gate,
                            'research': opportunity.research.model_dump(mode='json') if opportunity.research else None,
                            'research_evidence_urls': opportunity.research_evidence_urls,
                            'research_warnings': opportunity.research_warnings,
                            'score': opportunity.score.model_dump(mode='json') if opportunity.score else None,
                            'status': opportunity.status,
                        })
                    opportunity.strategy = generated.value.model_dump(mode='json')
                except Exception as exc:
                    errors.append(('strategy', opportunity.company_name, type(exc).__name__))
                    error_labels.append(f'strategy: {opportunity.company_name} ({type(exc).__name__})')

        diagnostic_failure_counts = Counter(error_type for stage, _subject, error_type in errors
                                            if stage == 'diagnostic')
        if diagnostic_failure_counts:
            summary = ', '.join(f'{category}={count}' for category, count
                                in sorted(diagnostic_failure_counts.items()))
            error_labels.append(f'diagnostic failure categories: {summary}')

        summary_data = {
            'opportunity_count': len(opportunities),
            'gate': {key: gate_counts.get(key, 0) for key in ('research', 'hold', 'drop')},
            'research': {
                'attempted': research_attempted, 'success': research_success,
                'failed': sum(research_failure_counts.values()),
                'failure_categories': dict(sorted(research_failure_counts.items())),
            },
            'scoring': {'attempted': scoring_attempted, 'success': scoring_success,
                        'failed': max(scoring_attempted - scoring_success, 0)},
            'top_candidates': len(top_opportunities),
            'runtime_seconds': round(time.monotonic() - started_monotonic, 2),
        }
        log.info('daily run summary run_id=%s status=%s opportunities=%d gate=%s research=%s scoring=%s '
                 'top_candidates=%d runtime_seconds=%.2f',
                 run.id, 'partial' if errors else 'completed', len(opportunities),
                 summary_data['gate'], summary_data['research'], summary_data['scoring'],
                 len(top_opportunities), summary_data['runtime_seconds'])

        run = repository.save_run_results(run_id=run.id, settings=settings,
            prefilter_decisions=prefilter_decisions,
            processed_raw_item_ids=processed_raw_item_ids, opportunities=opportunities, errors=errors,
            error_details=error_details, warnings=warning_labels, summary=summary_data)
        report = format_opportunity_report(run, top_opportunities,
            str(datetime.now(ZoneInfo(settings.timezone)).date()))
        if error_labels:
            await send_discord_report(format_error_report(run.id, run.status, error_labels),
                                      settings.discord_webhook_url.get_secret_value())
        if warning_labels:
            await send_discord_report(format_warning_report(run.id, warning_labels),
                                      settings.discord_webhook_url.get_secret_value())
        await send_discord_report(report, settings.discord_webhook_url.get_secret_value())
        return run.status == 'completed'
    except asyncio.CancelledError:
        repository.mark_failed(run.id, 'CancelledError')
        raise
    except Exception as exc:
        error_name = type(exc).__name__
        errors.append(('daily_pipeline', 'run', error_name))
        error_labels.append(f'daily_pipeline: {error_name}')
        log.error('daily pipeline failed run_id=%s type=%s message=%s stack=%s',
                  run.id, error_name, safe_exception_message(exc), safe_exception_traceback(exc))
        repository.mark_failed(run.id, error_name)
        await send_discord_report(format_error_report(run.id, 'failed', error_labels),
                                  settings.discord_webhook_url.get_secret_value())
        return False
    finally:
        if owned_client:
            await client.close()
        factory.kw['bind'].dispose()


def _evaluated_prefilter_events(raw_items: list[RawItem],
                                decisions: dict[int, PrefilterDecision]):
    from app.prefilters import EvaluatedEvent
    return [EvaluatedEvent(item.id, item.source_type, item.source_name, item.title, decisions[item.id])
            for item in raw_items if item.id in decisions]


async def run_v2_pipeline(settings: Settings, client: LLMClient | None = None, **kwargs) -> bool:
    """Compatibility name retained for scripts using the V2.1 entry point."""
    kwargs.setdefault('collect', True)
    return await run_daily_pipeline(settings, client, **kwargs)
