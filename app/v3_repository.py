"""Persistence adapter for the additive V3 pipeline."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import (ProcessingError, Run, V3CandidatePool,
                        V3FinalSelection, V3SalesMemo, V3ShortlistDecision,
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
            'v3_research_web_search', 'v3_final_selector_model',
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

    def save_candidate_pool(self, run_id: int, records: list[dict], raw_report: str) -> None:
        with self.factory.begin() as session:
            for record in records:
                session.add(V3CandidatePool(
                    run_id=run_id, company_id=record['company_id'],
                    company_name=record['company_name'],
                    discovery_origins=[record['discovery_source']],
                    payload_json=record, raw_report=raw_report))

    def save_shortlist(self, run_id: int, records: list[dict], selected: list[dict],
                       settings: Settings) -> None:
        selected_by_id = {item['company_id']: item for item in selected}
        with self.factory.begin() as session:
            for record in records:
                item = selected_by_id.get(record['company_id'])
                decision = item or {'company_id': record['company_id'],
                                    'company_name': record['company_name'],
                                    'why_selected': '', 'what_to_investigate': []}
                session.add(V3ShortlistDecision(
                    run_id=run_id, company_id=record['company_id'],
                    company_name=record['company_name'], selected=item is not None,
                    decision_json=decision, model=settings.v3_shortlist_model,
                    prompt_version=settings.v3_shortlist_prompt_version,
                    reasoning_effort=settings.v3_shortlist_reasoning_effort))

    def save_memo(self, run_id: int, memo: dict, evidence_urls: list[str],
                  warnings: list[str], settings: Settings) -> None:
        with self.factory.begin() as session:
            session.add(V3SalesMemo(
                run_id=run_id, company_id=memo['company_id'],
                company_name=memo['company_name'], memo_json=memo,
                evidence_urls=evidence_urls, warnings=warnings,
                model=settings.v3_research_model,
                prompt_version=settings.v3_research_prompt_version,
                reasoning_effort=settings.v3_research_reasoning_effort))

    def save_final(self, run_id: int, selected: list[dict], settings: Settings) -> None:
        with self.factory.begin() as session:
            for item in selected:
                session.add(V3FinalSelection(
                    run_id=run_id, company_id=item['company_id'],
                    company_name=item['company_name'], rank=item['rank'],
                    selection_json=item, model=settings.v3_final_selector_model,
                    prompt_version=settings.v3_final_selector_prompt_version,
                    reasoning_effort=settings.v3_final_selector_reasoning_effort))

    def finish(self, run_id: int, *, settings: Settings, candidate_count: int,
               shortlisted: int, research_success: int, final_count: int,
               errors: list[tuple[str, str, str]], summary: dict[str, Any]) -> Run:
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
                session.add(ProcessingError(
                    run_id=run_id, stage=stage, subject=subject,
                    error_type=error_type, exception_type=error_type,
                    error_message=None, model=model, prompt_version=prompt))
            session.flush()
            return run

    def mark_failed(self, run_id: int, error_type: str) -> None:
        with self.factory.begin() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.status = 'failed'
                run.finished_at = utcnow()
                run.error_message = error_type
