"""Persistence boundary for V2.2 pipeline inputs and completed run results."""
from __future__ import annotations

from datetime import datetime, time, timezone
import hashlib
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.deduplication import canonical_url, normalize_name, website_domain
from app.domain import Opportunity, RawItem
from app.models import (CandidateRecord, Company, Diagnostic, ProcessingError, ResearchScore, Run,
                        SourceEvent, SourceEventPrefilter, Strategy, Trigger, VCProfile, utcnow)
from app.prefilters import PrefilterDecision
from app.vc_profiles import profile_context_record

def _persist_event_candidates(session, opportunities: list[Opportunity], run_id: int,
                              settings: Settings) -> dict[int, tuple[Company, Trigger]]:
    """Resolve identities and existing triggers in batches, not one query per Event."""
    entries = []
    for opportunity in opportunities:
        for event in opportunity.events:
            candidate = event.to_candidate()
            source_type = event.source_type
            entries.append({
                'event': event,
                'candidate': candidate,
                'domain': website_domain(str(candidate.website)) if candidate.website else None,
                'normalized_name': normalize_name(candidate.company_name),
                'source_url': canonical_url(str(candidate.source_url)),
                'fingerprint': hashlib.sha256('|'.join((candidate.trigger_type,
                    normalize_name(candidate.trigger_title), str(candidate.published_at))).encode()).hexdigest(),
                'model': settings.discovery_model if source_type == 'web_search' else settings.fixed_discovery_model,
                'prompt_version': settings.discovery_prompt_version if source_type == 'web_search'
                    else settings.fixed_discovery_prompt_version,
            })
    if not entries:
        return {}

    domains = {entry['domain'] for entry in entries if entry['domain']}
    names = {entry['normalized_name'] for entry in entries}
    identity_filters = []
    if domains:
        identity_filters.append(Company.website_domain.in_(domains))
    if names:
        identity_filters.append(Company.normalized_name.in_(names))
    companies = list(session.scalars(select(Company).where(or_(*identity_filters))))
    companies_by_domain = {company.website_domain: company for company in companies if company.website_domain}
    companies_by_name: dict[str, list[Company]] = {}
    for company in companies:
        companies_by_name.setdefault(company.normalized_name, []).append(company)

    now = utcnow()
    for entry in entries:
        candidate, domain, normalized_name = entry['candidate'], entry['domain'], entry['normalized_name']
        company = companies_by_domain.get(domain) if domain else None
        if company is None:
            matches = companies_by_name.get(normalized_name, [])
            if domain:
                matches = [item for item in matches if not item.website_domain or item.website_domain == domain]
            if len(matches) == 1:
                company = matches[0]
        if company is None:
            company = Company(name=candidate.company_name, normalized_name=normalized_name,
                website=str(candidate.website) if candidate.website else None,
                website_domain=domain, location=candidate.location)
            session.add(company)
            companies_by_name.setdefault(normalized_name, []).append(company)
            if domain:
                companies_by_domain[domain] = company
        elif domain and not company.website_domain:
            company.website_domain, company.website = domain, str(candidate.website)
            companies_by_domain[domain] = company
        company.last_seen_at = now
        entry['company'] = company

    session.flush()
    company_ids = {entry['company'].id for entry in entries}
    existing_triggers = session.scalars(select(Trigger).where(Trigger.company_id.in_(company_ids))).all()
    by_fingerprint = {(trigger.company_id, trigger.fingerprint): trigger for trigger in existing_triggers}
    by_source_and_type = {(trigger.company_id, trigger.source_url, trigger.trigger_type): trigger
                          for trigger in existing_triggers}
    resolved: dict[int, tuple[Company, Trigger]] = {}
    for entry in entries:
        company = entry['company']
        candidate = entry['candidate']
        key = (company.id, entry['fingerprint'])
        trigger = by_fingerprint.get(key) or by_source_and_type.get(
            (company.id, entry['source_url'], candidate.trigger_type))
        if trigger is None:
            trigger = Trigger(company_id=company.id, trigger_type=candidate.trigger_type,
                title=candidate.trigger_title, summary=candidate.trigger_summary,
                source_url=entry['source_url'], source_title=candidate.source_title,
                published_at=datetime.combine(candidate.published_at, time(), timezone.utc)
                    if candidate.published_at else None,
                discovered_at=candidate.discovered_at or now, run_id=run_id,
                fingerprint=entry['fingerprint'], possible_video_need=candidate.possible_video_need,
                model=entry['model'], prompt_version=entry['prompt_version'],
                evidence_urls=sorted({str(item.source_url) for item in entry['event'].evidence}
                                     | {str(entry['event'].source_url)}))
            session.add(trigger)
            by_fingerprint[key] = trigger
            by_source_and_type[(company.id, entry['source_url'], candidate.trigger_type)] = trigger
        resolved[id(entry['event'])] = (company, trigger)
    session.flush()
    return resolved


class V2Repository:
    """Reads pipeline inputs once and persists decisions/results at boundaries."""

    def __init__(self, factory: sessionmaker) -> None:
        self.factory = factory

    def start_run(self, settings: Settings, *, collect: bool, web_discovery: bool,
                  fixed_discovery: bool, topics: list[str] | None) -> Run:
        config_keys = (
            'discovery_model', 'fixed_discovery_model', 'cheap_win_model', 'diagnostic_model', 'scoring_model',
            'strategy_model', 'discovery_prompt_version', 'fixed_discovery_prompt_version',
            'cheap_win_prompt_version', 'diagnostic_prompt_version', 'scoring_prompt_version',
            'strategy_prompt_version',
            'discovery_reasoning_effort', 'fixed_discovery_reasoning_effort', 'cheap_win_reasoning_effort',
            'diagnostic_reasoning_effort', 'scoring_reasoning_effort', 'win_pre_drop_threshold',
            'win_pre_diagnostic_threshold', 'gate_research_event_strength', 'peer_research_enabled',
            'diagnostic_include_hold',
            'diagnostic_web_search', 'source_prefilter_batch_size', 'fixed_discovery_include_hold_events',
            'atpress_prefilter_pass_score', 'atpress_prefilter_drop_score', 'fixed_discovery_batch_size',
            'top_candidates', 'timezone', 'fixed_collector_interval_hours',
        )
        run = Run(config_json={key: getattr(settings, key) for key in config_keys})
        run.config_json.update(pipeline='v2.2', collect=collect, web_discovery=web_discovery,
                               fixed_discovery=fixed_discovery, topics=topics)
        with self.factory.begin() as session:
            session.add(run)
            session.flush()
        return run

    def load_pipeline_inputs(self, limit: int) -> tuple[list[RawItem], list[dict[str, Any]]]:
        """One DB read for pending raw items and the slow-changing VC master data."""
        with self.factory() as session:
            rows = session.scalars(select(SourceEvent).where(SourceEvent.processed_at.is_(None))
                .order_by(SourceEvent.published_at.desc(), SourceEvent.id.desc()).limit(limit)).all()
            raw_items = [RawItem(id=row.id, source_type=row.source_type, source_name=row.source_name,
                title=row.title, summary=row.summary, company_name=row.company_name,
                published_at=row.published_at, source_url=row.source_url,
                event_type=row.event_type, raw_data=dict(row.raw_data or {})) for row in rows]
            profiles = [profile_context_record(profile) for profile in session.scalars(select(VCProfile)).all()]
        return raw_items, profiles

    def save_run_results(self, *, run_id: int, settings: Settings,
                         prefilter_decisions: dict[int, PrefilterDecision], processed_raw_item_ids: set[int],
                         opportunities: list[Opportunity], errors: list[tuple[str, str, str]],
                         error_details: dict[tuple[str, str, str], dict[str, str]] | None = None,
                         warnings: list[str] | None = None, summary: dict[str, Any] | None = None) -> Run:
        """Save every run output in one transaction after Python-side transforms finish."""
        scores: list[ResearchScore] = []
        with self.factory.begin() as session:
            run = session.get(Run, run_id)
            now = utcnow()
            item_ids = set(prefilter_decisions) | processed_raw_item_ids
            raw_rows = {row.id: row for row in session.scalars(select(SourceEvent).where(SourceEvent.id.in_(item_ids))).all()} if item_ids else {}
            prefilter_rows = {row.source_event_id: row for row in session.scalars(select(SourceEventPrefilter).where(
                SourceEventPrefilter.source_event_id.in_(prefilter_decisions))).all()} if prefilter_decisions else {}
            for raw_item_id, decision in prefilter_decisions.items():
                row = prefilter_rows.get(raw_item_id)
                if row is None:
                    row = SourceEventPrefilter(source_event_id=raw_item_id)
                    session.add(row)
                row.status, row.reason, row.score = decision.status, decision.reason, decision.score
                row.rule, row.classified_event_type = decision.rule, decision.classified_event_type
                row.event_strength = decision.event_strength
                row.matched_positive_signals = list(decision.matched_positive_signals)
                row.matched_negative_signals = list(decision.matched_negative_signals)
                row.supporting_signals = list(decision.supporting_signals)
                row.prefiltered_at = now
                raw = raw_rows.get(raw_item_id)
                if raw is not None:
                    raw.event_type = decision.classified_event_type
                    if decision.status == 'DROP' or raw_item_id in processed_raw_item_ids:
                        raw.processed_at = now
            for raw_item_id in processed_raw_item_ids:
                raw = raw_rows.get(raw_item_id)
                if raw is not None:
                    raw.processed_at = now

            persisted_event_rows = _persist_event_candidates(session, opportunities, run_id, settings)
            record_entries = []
            for opportunity in opportunities:
                if not opportunity.events:
                    continue
                primary_event = max(opportunity.events,
                    key=lambda event: (len(event.research_facts), len(event.evidence), len(event.summary),
                                       event.published_at or datetime.min.date()))
                primary_candidate = primary_event.to_candidate()
                company, primary_trigger = persisted_event_rows[id(primary_event)]
                if company is None or primary_trigger is None:
                    continue
                hard_filter_json = None
                if opportunity.gate and 'hard_filter' in opportunity.gate:
                    hard_filter_json = opportunity.gate['hard_filter']
                record = CandidateRecord(run_id=run_id, company_id=company.id, trigger_id=primary_trigger.id,
                    status=opportunity.status, discovery_origins=opportunity.origins,
                    discovery_sources=opportunity.sources, candidate_json=primary_candidate.model_dump(mode='json'),
                    merged_json=opportunity.model_dump(), hard_filter_json=hard_filter_json,
                    win_pre_json=opportunity.gate.get('win_pre') if opportunity.gate else None,
                    dropped_reason=(opportunity.gate.get('reason') if opportunity.status == 'drop' and opportunity.gate else None))
                session.add(record)
                record_entries.append((opportunity, record, company, primary_trigger, primary_candidate))

            # IDs are needed only to attach diagnostics; flush all opportunity
            # snapshots together instead of flushing once per company.
            session.flush()
            for opportunity, record, company, primary_trigger, primary_candidate in record_entries:
                if opportunity.research is not None:
                    detail = opportunity.research
                    session.add(Diagnostic(candidate_id=record.id, company_id=company.id,
                        trigger_id=primary_trigger.id, run_id=run_id,
                        current_expression_json=detail.current_expression.model_dump(mode='json'),
                        expression_debt_json=detail.expression_debt.model_dump(mode='json'),
                        peer_gap_json=detail.peer_gap.model_dump(mode='json') if detail.peer_gap else None,
                        creative_lock_in_json=detail.creative_lock_in.model_dump(mode='json'),
                        evidence_urls=opportunity.research_evidence_urls, model=settings.diagnostic_model,
                        prompt_version=settings.diagnostic_prompt_version))
                if opportunity.score is not None and opportunity.final_score is not None:
                    score = ResearchScore(company_id=company.id, trigger_id=primary_trigger.id, run_id=run_id,
                        candidate_json=primary_candidate.model_dump(mode='json'),
                        evaluation_json=opportunity.score.model_dump(mode='json'), total_score=opportunity.final_score,
                        model=settings.scoring_model, prompt_version=settings.scoring_prompt_version, selected_rank=None)
                    session.add(score)
                    scores.append(score)
                    record.status = 'scored'
                if opportunity.strategy:
                    session.add(Strategy(company_id=company.id, trigger_id=primary_trigger.id, run_id=run_id,
                        content_json=opportunity.strategy, model=settings.strategy_model,
                        prompt_version=settings.strategy_prompt_version, evidence_urls=[]))
                    run.strategy_count += 1

            top_scores = sorted(scores, key=lambda score: (-score.total_score, score.company_id, score.trigger_id))[:settings.top_candidates]
            for rank, score in enumerate(top_scores, 1):
                score.selected_rank = rank
            run.candidate_count = len(opportunities)
            run.scored_count = len(scores)
            run.status = 'partial' if errors else 'completed'
            run.finished_at = now
            run.error_message = '; '.join(f'{stage}: {error_type}' for stage, _subject, error_type in errors) or None
            if summary is not None:
                run.config_json = {**(run.config_json or {}), 'summary': {
                    **summary, 'validation_warnings': len(warnings or [])}}
            for stage, subject, error_type in errors:
                metadata = (error_details or {}).get((stage, subject, error_type), {})
                if not isinstance(metadata, dict):
                    metadata = {}
                session.add(ProcessingError(run_id=run_id, stage=stage, subject=subject,
                    error_type=error_type,
                    exception_type=metadata.get('exception_type'),
                    error_message=metadata.get('message'),
                    model=getattr(settings, f'{stage}_model', ''),
                    prompt_version=getattr(settings, f'{stage}_prompt_version', '')))
        return run

    def mark_failed(self, run_id: int, error_type: str) -> Run | None:
        with self.factory.begin() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.status, run.finished_at, run.error_message = 'failed', utcnow(), error_type
            return run
