"""Readable V2.2 daily pipeline; V1's run_pipeline remains independent."""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from app.collectors import CollectionResult, collect_fixed_sources
from app.config import Settings
from app.database import init_database
from app.domain import Event, Opportunity, RawItem
from app.llm import LLMClient
from app.models import Run
from app.opportunity_stages import (gate_opportunities, research_opportunities,
                                    score_opportunities)
from app.prefilters import PrefilterDecision, log_prefilter_metrics
from app.report import (format_error_report, format_opportunity_report,
                        send_discord_report)
from app.strategy import generate_strategy
from app.v2_discovery import (discover_web_events, extract_fixed_events,
                              group_events_by_company, merge_events)
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
                errors.append(('fixed_discovery', 'fixed_source_events', 'RejectedEventOutput'))
                error_labels.append(f'fixed_discovery: rejected={rejected}')

        web_events: list[Event] = []
        if web_discovery:
            web_events, rejected = await discover_web_events(client, settings, topics)
            log.info('web event discovery events=%d rejected=%d', len(web_events), rejected)
            if rejected:
                errors.append(('web_discovery', 'web_events', 'RejectedEventOutput'))
                error_labels.append(f'web_discovery: rejected={rejected}')

        all_events = merge_events(fixed_events, web_events)
        opportunities = group_events_by_company(all_events)
        log.info('opportunity grouping events=%d companies=%d', len(all_events), len(opportunities))

        await gate_opportunities(opportunities, client, settings, vc_profiles, errors)
        for stage, subject, error_type in errors:
            if error_type not in {'CollectionError', 'RejectedEventOutput'}:
                error_labels.append(f'{stage}: {subject} ({error_type})')

        await research_opportunities(opportunities, client, settings, vc_profiles, errors)
        await score_opportunities(opportunities, client, settings, errors)
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
                                                        opportunity.final_score or 0)
                    opportunity.strategy = generated.value.model_dump(mode='json')
                except Exception as exc:
                    errors.append(('strategy', opportunity.company_name, type(exc).__name__))
                    error_labels.append(f'strategy: {opportunity.company_name} ({type(exc).__name__})')

        run = repository.save_run_results(run_id=run.id, settings=settings,
            prefilter_decisions=prefilter_decisions,
            processed_raw_item_ids=processed_raw_item_ids, opportunities=opportunities, errors=errors)
        report = format_opportunity_report(run, top_opportunities,
            str(datetime.now(ZoneInfo(settings.timezone)).date()))
        if error_labels:
            await send_discord_report(format_error_report(run.id, run.status, error_labels),
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
        log.exception('daily pipeline failed run_id=%s type=%s', run.id, error_name)
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
