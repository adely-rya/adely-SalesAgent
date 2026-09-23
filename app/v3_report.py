"""Compact, human-facing Discord rendering for V3."""
from __future__ import annotations

from typing import Any

from app.report import send_discord_report, split_messages
from app.v3_scout import display_origin


def _short(value: Any, limit: int = 220) -> str:
    text = ' '.join(str(value or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _bullets(values: list[Any], limit: int, length: int = 220) -> list[str]:
    return [f'・{_short(value, length)}' for value in values[:limit] if str(value or '').strip()]


def format_v3_messages(day: str, summary: dict[str, Any], selected: list[dict[str, Any]],
                       run_id: int) -> list[str]:
    lines = [f'Sales Agent V3 — {day}', '', f'Status: {summary.get("status", "Completed")}',
             f'Scout candidates: {summary.get("scout_candidates", 0)}',
             f'Research allocation: {summary.get("research_allocation_mode", "empty")} '
             f'(selected {summary.get("shortlisted", 0)})',
             f'Shortlisted: {summary.get("shortlisted", 0)}',
             f'Deep Research success: {summary.get("research_success", 0)} / '
             f'{summary.get("research_attempted", 0)}',
             f'Final selected: {len(selected)}', '', 'Found via']
    for key, label in (('fixed', 'Fixed Source'), ('web', 'Web Search'), ('combined', 'Fixed + Web')):
        lines.append(f'・{label}: {summary.get("origins", {}).get(key, 0)}')
    models = summary.get('models', {})
    lines += ['', 'Models', f'・Scout: {models.get("scout", "")}',
              f'・Ranking: {models.get("ranking", "")}',
              f'・Research: {models.get("research", "")}',
              f'・Final: {models.get("final", "")}',
              '', 'Runtime', f'・{summary.get("runtime_seconds", 0):g} sec']
    api_plan = summary.get('api_plan', {})
    lines += [f'・Terra ranking calls: {summary.get("shortlist_api_calls", 0)}',
              f'・Web Search calls: {api_plan.get("web_search_calls_total", "unknown")}']
    validation = summary.get('validation_warnings', 0)
    if validation:
        lines += ['', 'Validation', f'・{validation} evidence or output warnings summarized',
                  '・Research continued where possible']
    lines += ['', f'Run: {run_id}']
    messages = ['\n'.join(lines)]
    for item in selected:
        memo = item['memo']
        opportunity = item['opportunity']
        selection = item['selection']
        sources = []
        for event in opportunity.events:
            url = str(event.source_url)
            if url not in [source[1] for source in sources]:
                sources.append((event.source_title, url))
        for evidence in memo.get('evidence', []):
            url = evidence.get('source_url')
            if url and url not in [source[1] for source in sources]:
                sources.append((evidence.get('claim', 'Evidence'), url))
        lines = [f'#{selection["rank"]} {memo["company_name"]}', '', 'Found via',
                 f'・{display_origin(opportunity)}', '', 'Why now',
                 f'・{_short(selection.get("why_now") or memo.get("why_now"), 240)}',
                 '', 'What we learned']
        lines += _bullets(memo.get('what_we_learned', []), 4)
        lines += ['', 'Expression / Peer insight',
                  f'・Gap: {memo.get("expression_gap", "unknown")}',
                  f'・{_short(memo.get("expression_gap_reason"), 240)}',
                  f'・{_short(memo.get("peer_comparison"), 240)}', '', 'Opportunity',
                  f'・{_short(selection.get("proposed_angle") or memo.get("opportunity_hypothesis"), 260)}']
        risks = memo.get('reasons_not_to_pursue', [])
        if selection.get('main_risk'):
            risks = [selection['main_risk'], *risks]
        if risks:
            lines += ['', 'Watch'] + _bullets(risks, 2)
        lines += ['', 'Sources']
        lines += [f'・[{_short(title, 80)}]({url})' for title, url in sources[:5]]
        messages.append('\n'.join(lines))
    return messages


async def send_v3_report(day: str, summary: dict[str, Any], selected: list[dict[str, Any]],
                         run_id: int, webhook_url: str) -> int:
    count = 0
    statuses: list[int] = []
    message_count = 0
    for message in format_v3_messages(day, summary, selected, run_id):
        chunks = split_messages(message)
        count += len(chunks)
        message_count += 1
        statuses.extend(await send_discord_report(message, webhook_url))
    # Keep the historic integer return type while exposing details to callers
    # through the summary fields they can persist before sending.
    return count
