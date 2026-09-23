"""Replay one saved Gate input without discovery, research, or scoring calls.

This is intentionally a derived-report tool: it never writes a Run or changes
the saved CandidateRecord/Diagnostic/ResearchScore rows.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import date
import json
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import Settings
from app.database import init_database
from app.domain import Opportunity
from app.gating import gate_decision
from app.llm import LLMClient
from app.models import CandidateRecord, Run
from app.opportunity_stages import gate_opportunities
from app.report import format_error_report, format_opportunity_messages, send_discord_report
from app.schemas import Candidate, DiagnosticOutput, EvaluationOutput, Event

log = logging.getLogger(__name__)


class CountingGateClient:
    def __init__(self, client: LLMClient):
        self.client = client
        self.calls = 0

    def prompt(self, name: str) -> str:
        return self.client.prompt(name)

    async def generate(self, **kwargs):
        self.calls += 1
        return await self.client.generate(**kwargs)


def _old_research_records(factory, run_id: int) -> list[CandidateRecord]:
    with factory() as session:
        records = list(session.scalars(select(CandidateRecord).where(
            CandidateRecord.run_id == run_id)).all())
    return [record for record in records
            if (record.merged_json or {}).get('gate', {}).get('status') == 'research']


def find_original_run(factory, today: date) -> tuple[Run, list[CandidateRecord]]:
    with factory() as session:
        runs = list(session.scalars(select(Run).order_by(Run.id.desc())).all())
        for run in runs:
            started = run.started_at
            if started and started.astimezone(ZoneInfo('Asia/Tokyo')).date() != today:
                continue
            records = list(session.scalars(select(CandidateRecord).where(
                CandidateRecord.run_id == run.id)).all())
            old_research = [record for record in records
                            if (record.merged_json or {}).get('gate', {}).get('status') == 'research']
            if len(old_research) == 21:
                return run, old_research
    raise RuntimeError('Could not identify today\'s saved 21-company Gate run')


def opportunity_from_record(record: CandidateRecord) -> Opportunity:
    merged = record.merged_json or {}
    return Opportunity(
        company_name=record.candidate_json['company_name'],
        events=[Event.model_validate(item) for item in merged.get('events', [])],
        candidate=Candidate.model_validate(record.candidate_json),
        origins=list(record.discovery_origins or []),
        sources=list(record.discovery_sources or []),
    )


def reuse_downstream(opportunities: list[Opportunity], records: list[CandidateRecord]) -> list[str]:
    """Attach only serialized downstream results from an earlier saved run."""
    by_name = {record.candidate_json.get('company_name'): record for record in records}
    unavailable: list[str] = []
    for opportunity in opportunities:
        record = by_name.get(opportunity.company_name)
        merged = (record.merged_json if record else {}) or {}
        research = merged.get('research')
        score = merged.get('score')
        if research:
            opportunity.research = DiagnosticOutput.model_validate(research)
            opportunity.research_evidence_urls = list(merged.get('research_evidence_urls') or [])
            opportunity.research_warnings = list(merged.get('research_warnings') or [])
        if score:
            opportunity.score = EvaluationOutput.model_validate(score)
            opportunity.final_score = merged.get('final_score')
        if opportunity.research is None or opportunity.score is None:
            unavailable.append(opportunity.company_name)
    return unavailable


def replay_summary(original: Run, opportunities: list[Opportunity], gate_calls: int,
                   unavailable: list[str], runtime: float) -> dict:
    counts = Counter(item.status for item in opportunities)
    selected = [item for item in opportunities if item.status == 'research']
    scored = [item for item in selected if item.score is not None]
    return {
        'opportunity_count': len(opportunities),
        'gate': {key: counts.get(key, 0) for key in ('research', 'hold', 'drop')},
        'research': {'attempted': len(selected), 'success': len(scored),
                     'failed': len(selected) - len(scored)},
        'scoring': {'attempted': len(scored), 'success': len(scored), 'failed': 0},
        'top_candidates': min(5, len(scored)),
        'runtime_seconds': round(runtime, 2),
        'replay': True,
        'original_run_id': original.id,
        'original_gate': {'research': 21, 'hold': 0, 'drop': 0},
        'gate_batch_size': 5,
        'gate_api_requests': gate_calls,
        'new_web_search': 0,
        'diagnostic_api_requests': 0,
        'scoring_api_requests': 0,
        'downstream_unavailable': unavailable,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', type=int)
    parser.add_argument('--reuse-run-id', type=int, default=13)
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    settings = Settings.from_env()
    factory = init_database(settings.database_url)
    try:
        if args.run_id:
            with factory() as session:
                original = session.get(Run, args.run_id)
            if original is None:
                raise RuntimeError(f'Run {args.run_id} not found')
            original_records = _old_research_records(factory, original.id)
        else:
            original, original_records = find_original_run(
                factory, date.today())
        if len(original_records) != 21:
            raise RuntimeError(f'Run {original.id} has {len(original_records)} old RESEARCH records, expected 21')

        with factory() as session:
            reuse_records = list(session.scalars(select(CandidateRecord).where(
                CandidateRecord.run_id == args.reuse_run_id)).all())
        opportunities = [opportunity_from_record(record) for record in original_records]
        client = CountingGateClient(LLMClient(settings))
        errors: list[tuple[str, str, str]] = []
        import time
        started = time.monotonic()
        try:
            profiles = []
            await gate_opportunities(opportunities, client, settings, profiles, errors)
            old_by_name = {item.company_name: 'research' for item in opportunities}
            comparison = []
            for item in opportunities:
                comparison.append({
                    'company': item.company_name,
                    'old': old_by_name[item.company_name],
                    'new': item.status,
                    'win_pre': (item.gate or {}).get('win_pre', {}).get('win_pre'),
                    'confidence': (item.gate or {}).get('win_pre', {}).get('confidence'),
                    'trigger_quality': (item.gate or {}).get('win_pre', {}).get('trigger_quality'),
                    'target_fit': (item.gate or {}).get('win_pre', {}).get('target_fit'),
                    'research_value': (item.gate or {}).get('win_pre', {}).get('research_value'),
                    'reason': (item.gate or {}).get('reason', ''),
                })
            selected = [item for item in opportunities if item.status == 'research']
            unavailable = reuse_downstream(selected, reuse_records)
            for item in selected:
                if item.research is None or item.score is None:
                    item.status = 'hold'
            selected = [item for item in opportunities if item.status == 'research' and item.score is not None]
            selected.sort(key=lambda item: (-(item.final_score or 0), item.company_name))
            summary = replay_summary(original, opportunities, client.calls, unavailable,
                                     time.monotonic() - started)
            print(json.dumps({'original_run_id': original.id, 'summary': summary,
                              'comparison': comparison}, ensure_ascii=False, indent=2))
            if args.send:
                fake_run = Run(id=original.id, status='completed', candidate_count=len(opportunities),
                               scored_count=len(selected), config_json={'summary': summary})
                messages = format_opportunity_messages(fake_run, selected[:5],
                                                       date.today().isoformat())
                title = (f'Gate Replay — {date.today().isoformat()}\n'
                         'No new Web Search / Research / Scoring')
                gate = summary['gate']
                title += (f'\n\nOriginal Gate\n・Research 21\n・Hold 0\n\n'
                          f'New Batched Gate\n・Research {gate["research"]}\n'
                          f'・Hold {gate["hold"]}\n・Drop {gate["drop"]}\n\n'
                          f'Batch size\n・5\nReasoning\n・High\n\n'
                          f'Gate API requests\n・{client.calls}\nNew Web Search\n・0\n'
                          'Research API\n・0\nScoring API\n・0')
                await send_discord_report(title, settings.discord_webhook_url.get_secret_value())
                for message in messages[1:]:
                    await send_discord_report(message, settings.discord_webhook_url.get_secret_value())
                if errors or unavailable:
                    labels = [f'Gate Replay: {name} (downstream unavailable)'
                              for name in unavailable]
                    await send_discord_report(format_error_report(
                        original.id, 'partial', labels), settings.discord_webhook_url.get_secret_value())
        finally:
            await client.client.close()
    finally:
        factory.kw['bind'].dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
