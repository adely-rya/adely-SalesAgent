"""V2 discovery pipeline.  V1's run_pipeline intentionally remains unchanged."""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from app.collectors import CollectionResult, collect_fixed_sources
from app.config import Settings
from app.database import init_database
from app.deduplication import persist_candidate
from app.diagnostics import diagnostic_context, run_diagnostic_research
from app.discovery import DISCOVERY_TOPICS, discover_topic
from app.gating import evaluate_cheap_win, gate_status, hard_filter
from app.llm import LLMClient
from app.models import (CandidateRecord, Diagnostic, ProcessingError, ResearchScore, Run,
                        SourceEvent, Strategy, utcnow)
from app.prefilters import (eligible_source_events, events_without_prefilter, log_prefilter_metrics,
                            prefilter_source_events, routing_metrics)
from app.report import format_error_report, format_report, send_discord_report
from app.scoring import evaluation_priority, score_company, select_top_candidates
from app.strategy import generate_strategy
from app.v2_discovery import (CandidateEnvelope, FixedDiscoveryResult, MergedCandidate,
                              discover_fixed_events, merge_candidates, web_envelope)
from app.vc_profiles import profile_context

log = logging.getLogger(__name__)


async def run_v2_pipeline(settings: Settings, client: LLMClient | None = None, *,
                          collect: bool = True, web_discovery: bool = True,
                          fixed_discovery: bool = True, topics: list[str] | None = None,
                          collector: Callable[..., Awaitable[CollectionResult]] = collect_fixed_sources,
                          web_discoverer: Callable[..., Awaitable[tuple[list, set[str], int]]] = discover_topic,
                          fixed_discoverer: Callable[..., Awaitable[FixedDiscoveryResult]] = discover_fixed_events) -> bool:
    """Run the V2 cost ladder; injectable stages keep all tests offline and free."""
    factory = init_database(settings.database_url)
    config_keys = (
        'discovery_model', 'fixed_discovery_model', 'cheap_win_model', 'diagnostic_model', 'scoring_model',
        'discovery_reasoning_effort', 'fixed_discovery_reasoning_effort', 'cheap_win_reasoning_effort',
        'diagnostic_reasoning_effort', 'scoring_reasoning_effort', 'win_pre_drop_threshold',
        'win_pre_diagnostic_threshold', 'peer_research_enabled', 'diagnostic_include_hold',
        'diagnostic_web_search', 'source_prefilter_batch_size',
        'fixed_discovery_include_hold_events', 'atpress_prefilter_pass_score',
        'atpress_prefilter_drop_score', 'top_candidates', 'timezone',
    )
    with factory.begin() as session:
        run = Run(config_json={key: getattr(settings, key) for key in config_keys})
        run.config_json.update(pipeline='v2', collect=collect, web_discovery=web_discovery,
                               fixed_discovery=fixed_discovery, topics=topics)
        session.add(run)
    run_id = run.id
    errors: list[str] = []
    owned_client = client is None
    notification_attempted = False

    def record_error(stage: str, subject: str, exc: Exception) -> None:
        name = type(exc).__name__
        errors.append(f'{stage}: {name}')
        log.error('v2 processing failed stage=%s subject=%s type=%s', stage, subject, name)
        with factory.begin() as session:
            session.add(ProcessingError(run_id=run_id, stage=stage, subject=subject, error_type=name,
                model=getattr(settings, f'{stage}_model', ''),
                prompt_version=getattr(settings, f'{stage}_prompt_version', '')))

    try:
        if not settings.has_api_key:
            log.error('OPENAI_API_KEY is not configured. Set OPENAI_API_KEY in .env.')
            raise ValueError('Missing API key')
        client = client or LLMClient(settings)
        envelopes: list[CandidateEnvelope] = []

        if collect:
            result = await collector(settings, factory)
            for error in result.errors:
                record_error('collector', error, RuntimeError('Collection failed'))

        if fixed_discovery:
            with factory.begin() as session:
                pending = events_without_prefilter(session, settings.source_prefilter_batch_size)
                evaluated = prefilter_source_events(session, pending, settings)
            log_prefilter_metrics(evaluated)
            with factory() as session:
                events = eligible_source_events(session, settings.fixed_discovery_batch_size,
                                                settings.fixed_discovery_include_hold_events, settings)
                metrics = routing_metrics(session, events)
                log.info('fixed discovery routing selected=%d selected_avg_strength=%.1f sources=%s',
                         metrics['selected'], metrics['selected_avg_strength'], metrics['sources'])
            if events:
                try:
                    result = await fixed_discoverer(client, settings, events)
                    envelopes.extend(result.candidates)
                    candidate_counts: dict[str, int] = {}
                    for candidate in result.candidates:
                        for provider in {source.provider for source in candidate.sources}:
                            candidate_counts[provider] = candidate_counts.get(provider, 0) + 1
                    for provider, count in candidate_counts.items():
                        log.info('fixed discovery source=%s candidates_generated=%d', provider, count)
                    with factory.begin() as session:
                        for event_id in result.processed_event_ids:
                            event = session.get(SourceEvent, event_id)
                            if event is not None:
                                event.processed_at = utcnow()
                except Exception as exc:
                    record_error('fixed_discovery', 'source_events', exc)

        if web_discovery:
            for topic in topics if topics is not None else DISCOVERY_TOPICS:
                try:
                    found, _evidence, rejected = await web_discoverer(client, settings, topic)
                    envelopes.extend(web_envelope(candidate) for candidate in found)
                    if rejected:
                        record_error('web_discovery', topic, ValueError('Rejected candidates'))
                except Exception as exc:
                    record_error('web_discovery', topic, exc)

        merged = merge_candidates(envelopes)
        with factory.begin() as session:
            session.get(Run, run_id).candidate_count = len(merged)
        log.info('v2 candidates merged=%d', len(merged))

        scored: list[ResearchScore] = []
        candidates_by_score: dict[int, tuple[MergedCandidate, CandidateRecord, dict]] = {}
        for opportunity in merged:
            try:
                source_urls = {source.url for source in opportunity.sources}
                with factory.begin() as session:
                    company = None
                    primary_trigger = None
                    for trigger_candidate in opportunity.triggers:
                        origin_model = (settings.fixed_discovery_model if all(source.type != 'web_search'
                                        for source in opportunity.sources) else settings.discovery_model)
                        company, trigger, _is_new = persist_candidate(
                            session, trigger_candidate, run_id, origin_model,
                            settings.fixed_discovery_prompt_version if origin_model == settings.fixed_discovery_model
                            else settings.discovery_prompt_version, source_urls)
                        if trigger_candidate == opportunity.primary:
                            primary_trigger = trigger
                    if company is None or primary_trigger is None:
                        raise ValueError('Merged candidate had no trigger')
                    record = CandidateRecord(run_id=run_id, company_id=company.id, trigger_id=primary_trigger.id,
                        status='merged', discovery_origins=opportunity.origins,
                        discovery_sources=[source.model_dump() for source in opportunity.sources],
                        candidate_json=opportunity.primary.model_dump(mode='json'),
                        merged_json=opportunity.model_dump())
                    session.add(record)
                    session.flush()
                    candidate_id, company_id, trigger_id = record.id, company.id, primary_trigger.id

                filter_result = hard_filter(opportunity)
                with factory.begin() as session:
                    record = session.get(CandidateRecord, candidate_id)
                    record.hard_filter_json = filter_result.model_dump()
                    record.status = 'hard_filtered' if filter_result.excluded else 'hard_filter_passed'
                    if filter_result.excluded:
                        record.dropped_reason = filter_result.reason
                if filter_result.excluded:
                    continue

                providers = {source.provider for source in opportunity.sources if source.type == 'vc_news'}
                with factory() as session:
                    profiles = profile_context(session, providers)
                cheap = await evaluate_cheap_win(client, settings, opportunity, profiles, filter_result)
                cheap_json = cheap.value.model_dump()
                status = gate_status(cheap.value, settings)
                with factory.begin() as session:
                    record = session.get(CandidateRecord, candidate_id)
                    record.win_pre_json = cheap_json
                    record.status = status
                    if status == 'dropped':
                        record.dropped_reason = cheap.value.reason
                if status == 'dropped' or (status == 'hold' and not settings.diagnostic_include_hold):
                    continue

                diagnostic = await run_diagnostic_research(client, settings, opportunity, cheap_json, profiles)
                diagnostic_json = diagnostic_context(diagnostic.value)
                with factory.begin() as session:
                    session.add(Diagnostic(candidate_id=candidate_id, company_id=company_id, trigger_id=trigger_id,
                        run_id=run_id, current_expression_json=diagnostic_json['current_expression'],
                        expression_debt_json=diagnostic_json['expression_debt'], peer_gap_json=diagnostic_json['peer_gap'],
                        creative_lock_in_json=diagnostic_json['creative_lock_in'],
                        evidence_urls=sorted(diagnostic.evidence_urls), model=settings.diagnostic_model,
                        prompt_version=settings.diagnostic_prompt_version))
                    session.get(CandidateRecord, candidate_id).status = 'diagnosed'

                score_context = {
                    'discovery_origins': opportunity.origins,
                    'discovery_sources': [source.model_dump() for source in opportunity.sources],
                    'discovery_reasons': list(opportunity.discovery_reasons), 'hard_filter': filter_result.model_dump(),
                    'win_pre': cheap_json, 'vc_profiles': profiles, 'diagnostic': diagnostic_json,
                }
                evaluation = await score_company(client, settings, opportunity.primary, score_context)
                score = ResearchScore(company_id=company_id, trigger_id=trigger_id, run_id=run_id,
                    candidate_json=opportunity.primary.model_dump(mode='json'), evaluation_json=evaluation.model_dump(),
                    total_score=evaluation_priority(evaluation), model=settings.scoring_model,
                    prompt_version=settings.scoring_prompt_version)
                with factory.begin() as session:
                    session.add(score)
                    session.flush()
                    session.get(CandidateRecord, candidate_id).status = 'scored'
                scored.append(score)
                candidates_by_score[score.id] = (opportunity, record, score_context)
            except Exception as exc:
                record_error('v2_candidate', opportunity.primary.company_name, exc)

        top = select_top_candidates(scored, settings.top_candidates)
        with factory.begin() as session:
            run = session.get(Run, run_id)
            run.scored_count = len({score.company_id for score in scored})
            for rank, score in enumerate(top, 1):
                session.get(ResearchScore, score.id).selected_rank = rank
        entries = []
        for score in top:
            opportunity, record, context = candidates_by_score[score.id]
            entry = {'candidate': opportunity.primary, 'score': score, 'strategy_skipped': not settings.v2_generate_strategy,
                     'v2': {'origins': opportunity.origins, 'win_pre': context['win_pre']}}
            entries.append(entry)
            if not settings.v2_generate_strategy:
                continue
            try:
                generated = await generate_strategy(client, settings, opportunity.primary, score)
                with factory.begin() as session:
                    session.add(Strategy(company_id=score.company_id, trigger_id=score.trigger_id,
                        content_json=generated.value.model_dump(), model=settings.strategy_model,
                        prompt_version=settings.strategy_prompt_version, run_id=run_id,
                        evidence_urls=sorted(generated.evidence_urls)))
                    session.get(Run, run_id).strategy_count += 1
                entry['strategy'] = generated.value
            except Exception as exc:
                record_error('strategy', opportunity.primary.company_name, exc)

        with factory.begin() as session:
            session.get(Run, run_id).status = 'partial' if errors else 'completed'
        report = format_report(run, entries, str(datetime.now(ZoneInfo(settings.timezone)).date()))
        try:
            if errors:
                notification_attempted = True
                await send_discord_report(format_error_report(run_id, 'partial', errors),
                                          settings.discord_webhook_url.get_secret_value())
            notification_attempted = True
            await send_discord_report(report, settings.discord_webhook_url.get_secret_value())
        except Exception as exc:
            record_error('discord', 'report', exc)
    except asyncio.CancelledError:
        record_error('pipeline', 'run', RuntimeError('Run cancelled'))
        with factory.begin() as session:
            session.get(Run, run_id).status = 'failed'
        raise
    except Exception as exc:
        record_error('pipeline', 'run', exc)
        with factory.begin() as session:
            session.get(Run, run_id).status = 'failed'
        if not notification_attempted:
            try:
                notification_attempted = True
                await send_discord_report(format_error_report(run_id, 'failed', errors),
                                          settings.discord_webhook_url.get_secret_value())
            except Exception as notify_exc:
                record_error('discord', 'error_report', notify_exc)
    finally:
        with factory.begin() as session:
            run = session.get(Run, run_id)
            if run.status != 'failed':
                run.status = 'partial' if errors else 'completed'
            run.finished_at = utcnow()
            run.error_message = '; '.join(errors) if errors else None
        if owned_client and client is not None:
            await client.close()
        final_status = run.status
        factory.kw['bind'].dispose()
    return final_status == 'completed'
