"""Read-only sales recommendations, delivered only to the configured Discord webhook."""
import asyncio
import logging
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
    """Render scored V2.2 company aggregates without ORM-specific score access."""
    lines = ['# adely Sales Report', day, f'Run: {run.id} / {run.status}',
             f'営業候補Opportunity: {run.candidate_count}', f'評価企業: {run.scored_count}',
             f'Top candidates: {len(opportunities)}', f'戦略生成: {run.strategy_count}']
    summary = (run.config_json or {}).get('summary', {})
    if summary:
        gate = summary.get('gate', {})
        research = summary.get('research', {})
        scoring = summary.get('scoring', {})
        lines += [f"Gate: RESEARCH={gate.get('research', 0)} HOLD={gate.get('hold', 0)} DROP={gate.get('drop', 0)}",
                  f"Research: attempted={research.get('attempted', 0)} success={research.get('success', 0)} "
                  f"failed={research.get('failed', 0)}",
                  f"Scoring: attempted={scoring.get('attempted', 0)} success={scoring.get('success', 0)} "
                  f"failed={scoring.get('failed', 0)}",
                  f"Validation warnings: {summary.get('validation_warnings', 0)}",
                  f"Runtime: {summary.get('runtime_seconds', 0):g}s"]
    for rank, opportunity in enumerate(opportunities, 1):
        evaluation = opportunity.score.model_dump(mode='json')
        win_pre = opportunity.gate['win_pre']
        lines += ['', f'## {rank}. {opportunity.company_name}',
                  f'Score: {opportunity.final_score:g} / 100', '', '営業トリガー',
                  opportunity.candidate.trigger_title,
                  'Why now', opportunity.candidate.trigger_summary,
                  f"Discovery: {', '.join(opportunity.origins)}",
                  f"WIN Pre: {win_pre['win_pre']:g}/10 ({win_pre['confidence']})",
                  f"WIN Unknowns: {', '.join(win_pre.get('unknown_factors', [])) or 'なし'}",
                  'Why us / Entry Potential', evaluation['reason'],
                  'Suggested video idea', evaluation['scope_hypothesis'],
                  '参考順位点（受注確率ではありません）']
        for stage in ('need', 'win', 'deliver'):
            mean = sum(evaluation[stage].values()) / 5
            evidence = evaluation['evidence_coverage'][stage]
            lines += [f"{stage.upper()}: {mean:g}/10 / 根拠: {evidence['coverage']}", evidence['reason']]
        lines += ['リスク']
        for risk in evaluation['risks']:
            sources = ', '.join(str(url) for url in risk['source_urls'])
            lines.append(f"{risk['axis'].upper()} / {risk['severity']} / {risk['risk']}"
                         f" / score={risk['score_impact']} / evidence={risk['evidence']} / {sources}")
        lines += ['Unknown', *evaluation.get('unknowns', []), '追加確認', *evaluation['research_needed']]
        if opportunity.strategy:
            strategy = opportunity.strategy
            lines += ['', 'なぜ今', strategy['why_now'], '', '提案', strategy['proposal']['title'],
                strategy['proposal']['description'], *['・' + item for item in strategy['proposal']['deliverables']],
                '', '想定価格（仮説）', strategy['estimated_budget'], '', '接触先（提案）',
                strategy['target_department'] + ' / ' + strategy['target_role'], strategy['first_contact_method'],
                '', '営業の切り口', strategy['sales_angle'], '', 'リスク',
                *['・' + risk for risk in strategy['risks']]]
        lines += ['', 'Sources:', *[source['url'] for source in opportunity.sources]]
        if opportunity.research_evidence_urls:
            lines += ['Research Sources:', *opportunity.research_evidence_urls]
        if opportunity.research_warnings:
            lines += ['Research warnings:', *opportunity.research_warnings]
    return '\n'.join(lines)


def format_error_report(run_id: int, status: str, errors: list[str]) -> str:
    """Create a safe alert without exception bodies or credentials."""
    return '\n'.join([
        '# adely Sales Agent Error', f'Run: {run_id}', f'Status: {status}', '',
        '処理中にエラーが発生しました。',
        *[f'・{error}' for error in errors],
    ])


def format_warning_report(run_id: int, warnings: list[str]) -> str:
    return '\n'.join([
        '# adely Sales Agent Validation Summary', f'Run: {run_id}', '',
        'Validation warnings (not system failures):',
        *[f'・{warning}' for warning in warnings],
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


async def send_discord_report(report: str, webhook_url: str) -> None:
    if not configured(webhook_url):
        log.info('discord notification skipped (webhook not configured)\n%s', report)
        return
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
                break
    log.info('discord notification delivered')
