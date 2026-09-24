"""Persistence adapter for the additive V3 pipeline."""
from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.listing_master import ListingMatch
from app.models import (ProcessingError, Run, V3CandidatePool,
                        V3FinalSelection, V3ListingPolicyDecision, V3SalesMemo,
                        V3ShortlistDecision,
                        utcnow)


class V3Repository:
    def __init__(self, factory: sessionmaker) -> None:
        self.factory = factory

    def start_run(self, settings: Settings, *, collect: bool,
                  web_discovery: bool, fixed_discovery: bool,
                  topics: list[str] | None) -> Run:
        keys = (
            'sales_pipeline_version', 'v3_shortlist_model',
            'v3_shortlist_reasoning_effort', 'v3_shortlist_size',
            'v3_shortlist_prompt_version', 'v3_research_model',
            'v3_research_reasoning_effort', 'v3_research_prompt_version',
            'v3_research_web_search', 'v3_research_concurrency',
            'v3_final_selector_model',
            'v3_final_selector_reasoning_effort', 'v3_final_size',
            'v3_final_selector_prompt_version', 'discovery_model',
            'fixed_discovery_model', 'discovery_prompt_version',
            'fixed_discovery_prompt_version', 'discovery_reasoning_effort',
            'fixed_discovery_reasoning_effort', 'timezone',
        )
        config = {key: getattr(settings, key) for key in keys}
        config.update(pipeline='v3', collect=collect, web_discovery=web_discovery,
                      fixed_discovery=fixed_discovery, topics=topics)
        run = Run(config_json=config)
        with self.factory.begin() as session:
            session.add(run)
            session.flush()
        return run

    def save_candidate_pool(self, run_id: int, records: list[dict], raw_report: str,
                            candidate_ref_mapping: dict[str, str]) -> None:
        with self.factory.begin() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise ValueError(f'Unknown V3 run {run_id}')
            run.config_json = {**(run.config_json or {}),
                               'candidate_ref_mapping': candidate_ref_mapping}
            for record in records:
                session.add(V3CandidatePool(
                    run_id=run_id, company_id=record['company_id'],
                    company_name=record['company_name'],
                    discovery_origins=[record['discovery_source']],
                    payload_json=record, raw_report=raw_report))

    def save_listing_policy(self, run_id: int, decisions: list[ListingMatch]) -> None:
        with self.factory.begin() as session:
            for decision in decisions:
                session.add(V3ListingPolicyDecision(
                    run_id=run_id, candidate_ref=decision.candidate_ref,
                    company_name=decision.company_name,
                    normalized_name=decision.normalized_name,
                    listing_match_status=decision.status.value,
                    matched_company_name=decision.matched_company_name,
                    security_code=decision.security_code,
                    market_segment=decision.market_segment,
                    master_as_of=decision.master_as_of,
                    policy_action=decision.policy_action,
                    phase=decision.phase))

    def save_shortlist(self, run_id: int, records: list[dict], selected: list[dict],
                       settings: Settings, *, allocation_mode: str) -> None:
        selected_by_ref = {item['candidate_ref']: item for item in selected}
        model = settings.v3_shortlist_model if allocation_mode == 'terra_ranked' else 'bypass'
        prompt_version = settings.v3_shortlist_prompt_version if allocation_mode == 'terra_ranked' else ''
        reasoning_effort = (settings.v3_shortlist_reasoning_effort
                            if allocation_mode == 'terra_ranked' else None)
        with self.factory.begin() as session:
            for record in records:
                item = selected_by_ref.get(record['candidate_ref'])
                decision = item or {'candidate_ref': record['candidate_ref'],
                                    'company_name': record['company_name'],
                                    'reason': ''}
                session.add(V3ShortlistDecision(
                    run_id=run_id, company_id=record['company_id'],
                    company_name=record['company_name'], selected=item is not None,
                    decision_json=decision, model=model,
                    prompt_version=prompt_version,
                    reasoning_effort=reasoning_effort))

    def save_memo(self, run_id: int, company_id: str, memo: dict, evidence_urls: list[str],
                  warnings: list[str], settings: Settings) -> None:
        persisted_memo = {key: value for key, value in memo.items()
                          if not key.startswith('_')}
        with self.factory.begin() as session:
            session.add(V3SalesMemo(
                run_id=run_id, company_id=company_id,
                company_name=persisted_memo['company_name'], memo_json=persisted_memo,
                evidence_urls=evidence_urls, warnings=warnings,
                model=settings.v3_research_model,
                prompt_version=settings.v3_research_prompt_version,
                reasoning_effort=settings.v3_research_reasoning_effort))

    def save_final(self, run_id: int, selected: list[dict],
                   candidate_ref_mapping: dict[str, str], settings: Settings) -> None:
        with self.factory.begin() as session:
            for item in selected:
                session.add(V3FinalSelection(
                    run_id=run_id, company_id=candidate_ref_mapping[item['candidate_ref']],
                    company_name=item['company_name'], rank=item['rank'],
                    selection_json=item, model=settings.v3_final_selector_model,
                    prompt_version=settings.v3_final_selector_prompt_version,
                    reasoning_effort=settings.v3_final_selector_reasoning_effort))

    def finish(self, run_id: int, *, settings: Settings, candidate_count: int,
               shortlisted: int, research_success: int, final_count: int,
               errors: list[tuple[str, str, str]], summary: dict[str, Any],
               error_details: list[dict[str, Any]] | None = None) -> Run:
        with self.factory.begin() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise ValueError(f'Unknown V3 run {run_id}')
            run.candidate_count = candidate_count
            run.scored_count = final_count
            run.status = 'partial' if errors else 'completed'
            run.finished_at = utcnow()
            run.error_message = '; '.join(
                f'{stage}: {error_type}' for stage, _subject, error_type in errors) or None
            run.config_json = {**(run.config_json or {}), 'summary': summary,
                               'v3_counts': {'shortlisted': shortlisted,
                                             'research_success': research_success,
                                             'final_selected': final_count}}
            for stage, subject, error_type in errors:
                model = getattr(settings, f'v3_{stage}_model', '')
                prompt = getattr(settings, f'v3_{stage}_prompt_version', '')
                detail = next((item for item in (error_details or [])
                               if item.get('company_name') == subject), None)
                session.add(ProcessingError(
                    run_id=run_id, stage=stage, subject=subject,
                    error_type=error_type, exception_type=error_type,
                    error_message=json.dumps(detail, ensure_ascii=False) if detail else None,
                    model=model, prompt_version=prompt))
            session.flush()
            return run

    def mark_failed(self, run_id: int, error_type: str) -> None:
        with self.factory.begin() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.status = 'failed'
                run.finished_at = utcnow()
                run.error_message = error_type
