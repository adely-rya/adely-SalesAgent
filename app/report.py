"""Read-only sales recommendations, delivered only to the configured Discord webhook."""
import asyncio
import logging
import re
from collections import OrderedDict
from typing import Any
import httpx
from app.config import configured
from app.models import Run

log = logging.getLogger(__name__)


def format_report(run: Run, entries: list[dict], day: str) -> str:
    lines = ['# adely Sales Report', day, f'Run: {run.id} / {run.status}',
             f'探索企業: {run.candidate_count}', f'評価企業: {run.scored_count}',
             f'Top candidates: {len(entries)}', f'戦略生成: {run.strategy_count}']
    for rank, entry in enumerate(entries, 1):
        candidate, score, strategy = entry['candidate'], entry['score'], entry.get('strategy')
        lines += ['', f'## {rank}. {candidate.company_name}',
                  f'Score: {score.total_score:g} / 100', '', '営業トリガー', candidate.trigger_title]
        if entry.get('v2'):
            v2 = entry['v2']
            lines += [f"Discovery: {', '.join(v2['origins'])}",
                      f"WIN Pre: {v2['win_pre']['win_pre']:g}/10 ({v2['win_pre']['confidence']})"]
        if hasattr(score, 'evaluation_json'):
            evaluation = score.evaluation_json
            lines += ['参考順位点（受注確率ではありません）', evaluation['scope_hypothesis']]
            for stage in ('need', 'win', 'deliver'):
                mean = sum(evaluation[stage].values()) / 5
                evidence = evaluation['evidence_coverage'][stage]
                lines += [f"{stage.upper()}: {mean:g}/10 / 根拠: {evidence['coverage']}", evidence['reason']]
            lines += ['リスク', *evaluation['risks'], '追加確認', *evaluation['research_needed']]
        if strategy:
            lines += ['', 'なぜ今', strategy.why_now, '', '提案', strategy.proposal.title,
                      strategy.proposal.description,
                      *['・' + item for item in strategy.proposal.deliverables],
                      '', '想定価格（仮説）', strategy.estimated_budget, '', '接触先（提案）',
                      strategy.target_department + ' / ' + strategy.target_role,
                      strategy.first_contact_method, '', '営業の切り口', strategy.sales_angle,
                      '', 'リスク', *['・' + risk for risk in strategy.risks]]
        elif not entry.get('strategy_skipped', False):
            lines += ['戦略生成に失敗しました。DBのprocessing_errorsを確認してください。']
        lines += ['', 'Source:', str(candidate.source_url)]
    return '\n'.join(lines)


def format_opportunity_report(run: Run, opportunities: list, day: str) -> str:
    """Render the complete daily brief as text for offline previews and tests."""
    return '\n\n'.join(format_opportunity_messages(run, opportunities, day))


def format_opportunity_messages(run: Run, opportunities: list, day: str) -> list[str]:
    """Render one concise summary message followed by one message per company."""
    messages = [_format_run_summary(run, len(opportunities), day)]
    messages.extend(_format_opportunity_message(rank, opportunity)
                   for rank, opportunity in enumerate(opportunities, 1))
    return messages


def _format_run_summary(run: Run, top_count: int, day: str) -> str:
    summary = (run.config_json or {}).get('summary', {})
    gate = summary.get('gate', {})
    research = summary.get('research', {})
    scoring = summary.get('scoring', {})
    validation = summary.get('validation', {})
    status = str(run.status or 'unknown').capitalize()
    lines = [f'Sales Agent — {day}', '', f'Status: {status}',
             f'Opportunities: {summary.get("opportunity_count", run.candidate_count)}', '',
             'Gate', f'・Research {gate.get("research", 0)}', f'・Hold {gate.get("hold", 0)}', '',
             'Research', f'・Success {research.get("success", 0)}',
             f'・Failed {research.get("failed", 0)}', '', 'Scoring',
             f'・Success {scoring.get("success", 0)}', f'・Failed {scoring.get("failed", 0)}', '',
             f'Top Candidates: {top_count}']
    removed = validation.get('evidence_urls_removed', 0)
    if removed:
        affected = validation.get('affected_companies')
        suffix = f' ({affected} companies)' if affected else ''
        lines += ['', 'Validation', f'・{removed} evidence URLs removed automatically{suffix}',
                  '・Research continued normally']
    elif summary.get('validation_warnings', 0):
        lines += ['', 'Validation',
                  f'・{summary["validation_warnings"]} validation warnings summarized automatically',
                  '・Research continued normally']
    runtime = summary.get('runtime_seconds', 0)
    lines += ['', f'Runtime: {runtime:g} sec']
    return '\n'.join(lines)


def _shorten(value: Any, limit: int = 170) -> str:
    text = str(value or '').strip()
    # Older research outputs occasionally copied a Markdown link into the
    # claim itself. Sources are rendered separately, so keep claims readable
    # and prevent internal evidence URLs from leaking into the brief.
    text = re.sub(r'\[([^\]\n]+)\]\(https?://[^)\s]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip('、。,. ') + '…'


def _bullet(value: Any, limit: int = 170) -> str:
    text = _shorten(value, limit)
    return f'・{text}' if text else ''


def _unique_bullets(values: list[Any], limit: int, length: int = 170) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        text = _shorten(value, length)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(f'・{text}')
        if len(result) >= limit:
            break
    return result


def _is_unknown_or_private(value: Any) -> bool:
    text = str(value or '')
    return bool(re.search(
        r'未確認|不明|確認できない|確認されていない|見つからない|予算|決裁者|発注時期|購買(?:窓口|経路)?|'
        r'新規制作会社|選定経路|外部制作会社.*(?:不明|未確認)|既存.*(?:不明|未確認)|'
        r'unknown|not (?:found|confirmed|known)|no evidence', text, re.I))


def _display_origins(opportunity) -> list[str]:
    origins = set(opportunity.origins or [])
    return ['Web Search' if origin == 'web_search' else 'Fixed Source'
            for origin in ('fixed', 'web_search')
            if (origin == 'web_search' and 'web_search' in origins)
            or (origin == 'fixed' and origins and any(item != 'web_search' for item in origins))]


def _observed_learning(opportunity) -> list[str]:
    values: list[str] = []
    for event in opportunity.events:
        values.extend(fact.fact for fact in event.research_facts
                      if not _is_unknown_or_private(fact.fact))
    research = opportunity.research
    if research:
        values.extend(item.claim for item in research.evidence
                      if item.evidence_type in {'observed', 'indirect'}
                      and not _is_unknown_or_private(item.claim))
        if research.current_expression.summary and not _is_unknown_or_private(
                research.current_expression.summary):
            values.append(research.current_expression.summary)
        values.extend(asset.observation for asset in research.current_expression.assets
                      if not _is_unknown_or_private(asset.observation))
    if not values:
        values.append(opportunity.candidate.trigger_summary or opportunity.candidate.trigger_title)
    return _unique_bullets(values, 4)


def _score_reason(evaluation: dict, stage: str) -> str:
    evidence = evaluation.get('evidence_coverage', {}).get(stage, {})
    reason = str(evidence.get('reason', '') or '')
    sentences = re.split(r'(?<=[。！？.!?])\s*', reason)
    observed = [sentence for sentence in sentences if sentence and not _is_unknown_or_private(sentence)]
    return _shorten(' '.join(observed), 135)


def _source_links(opportunity) -> list[str]:
    sources: OrderedDict[str, str] = OrderedDict()
    for event in opportunity.events:
        sources.setdefault(str(event.source_url), _shorten(event.source_title or 'Trigger', 70))
    for source in opportunity.sources or []:
        url = str(source.get('url', ''))
        if url:
            sources.setdefault(url, _shorten(source.get('provider') or 'Source', 70))
    for item in (opportunity.research.evidence if opportunity.research else []):
        if item.evidence_type != 'unknown' and item.source_url:
            sources.setdefault(str(item.source_url), 'Research evidence')
    return [f'・[{label}]({url})' for url, label in list(sources.items())[:5]]


def _format_opportunity_message(rank: int, opportunity) -> str:
    evaluation = opportunity.score.model_dump(mode='json')
    lines = [f'🥇 #{rank} {opportunity.company_name}',
             f'Score: {opportunity.final_score:g}', '', 'Found via']
    lines.extend(_unique_bullets(_display_origins(opportunity), 2))
    lines += ['', 'Trigger']
    trigger_values = [event.title for event in opportunity.events]
    lines.extend(_unique_bullets(trigger_values, 3))
    lines += ['', 'What we learned']
    lines.extend(_observed_learning(opportunity))
    lines += ['', 'Opportunity']
    idea = evaluation.get('scope_hypothesis') or opportunity.candidate.possible_video_need
    lines.append(_bullet(idea, 180))
    lines += ['', 'Score']
    for stage in ('need', 'win', 'deliver'):
        mean = sum(evaluation[stage].values()) / 5
        lines.append(f'{stage.upper()} {mean:.1f}')
        reason = _score_reason(evaluation, stage)
        if reason:
            lines.append(_bullet(reason, 135))
    watch: list[str] = []
    lock_in = opportunity.research.creative_lock_in if opportunity.research else None
    if lock_in and lock_in.status == 'likely':
        watch.append('Strong existing creative relationship: ' + lock_in.observation)
    for risk in evaluation.get('risks', []):
        value = risk.get('risk', '')
        if value and not _is_unknown_or_private(value):
            watch.append(value)
        if len(watch) >= 2:
            break
    if watch:
        lines += ['', 'Watch']
        lines.extend(_unique_bullets(watch, 2, 170))
    lines += ['', 'Sources']
    lines.extend(_source_links(opportunity) or [_bullet(opportunity.candidate.source_title, 80)])
    return '\n'.join(lines)


def format_error_report(run_id: int, status: str, errors: list[str]) -> str:
    """Create a safe alert without exception bodies or credentials."""
    groups: dict[str, list[str]] = {}
    for error in errors:
        match = re.match(r'([^:]+):\s*(.*?)\s*\([^)]*\)$', error)
        stage, subject = match.groups() if match else ('Processing', error)
        groups.setdefault(stage, [])
        if subject not in groups[stage]:
            groups[stage].append(subject)
    lines = [f'⚠ Processing errors: {len(errors)}', f'Run: {run_id}', f'Status: {status}', '']
    for stage, subjects in groups.items():
        label = {'diagnostic': 'Diagnostic', 'scoring': 'Scoring',
                 'discovery': 'Discovery', 'strategy': 'Strategy'}.get(stage, stage.title())
        lines.append(label)
        lines.extend(f'・{subject}' for subject in subjects[:8])
    lines += ['', 'Details: server logs']
    return '\n'.join(lines)


def format_warning_report(run_id: int, warnings: list[str]) -> str:
    return '\n'.join([
        'Validation', f'Run: {run_id}',
        f'・{len(warnings)} validation warnings summarized',
        '・Research continued normally',
    ])


def split_messages(text: str, limit: int = 1900) -> list[str]:
    """Split on readable boundaries without cutting URLs or Markdown links."""
    if limit <= 0:
        raise ValueError('limit must be positive')

    def units(value: str) -> int:
        return len(value.encode('utf-16-le')) // 2

    protected: list[tuple[int, int]] = []
    import re
    for match in re.finditer(r'\[[^\]\n]*\]\(https?://[^)\s]+\)|https?://[^\s<>]+', text):
        protected.append((match.start(), match.end()))

    def inside_protected(position: int) -> bool:
        return any(start < position < end for start, end in protected)

    chunks: list[str] = []
    start = 0
    while start < len(text):
        if units(text[start:]) <= limit:
            chunks.append(text[start:])
            break
        end = start
        while end < len(text) and units(text[start:end + 1]) <= limit:
            end += 1
        candidates = [index + 1 for index in range(start, end)
                      if text[index] in {'\n', ' ', '\t'} and not inside_protected(index + 1)]
        cut = max(candidates, default=end)
        if cut <= start:
            cut = end
        chunks.append(text[start:cut])
        start = cut
    return chunks


async def send_discord_report(report: str, webhook_url: str) -> list[int]:
    if not configured(webhook_url):
        log.info('discord notification skipped (webhook not configured)\n%s', report)
        return []
    statuses: list[int] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for chunk in split_messages(report):
            for attempt in range(4):
                response = await client.post(webhook_url, params={'wait': 'true'},
                    json={'content': chunk, 'allowed_mentions': {'parse': []}, 'flags': 4})
                if response.status_code == 429 and attempt < 3:
                    try:
                        delay = max(1, min(float(response.json().get('retry_after', 1)), 60))
                    except (ValueError, TypeError):
                        delay = 2 ** attempt
                    await asyncio.sleep(delay)
                    continue
                if response.status_code >= 500 and attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                statuses.append(response.status_code)
                break
    log.info('discord notification delivered chunks=%d statuses=%s', len(statuses), statuses)
    return statuses
