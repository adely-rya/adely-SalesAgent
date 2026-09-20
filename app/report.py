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
    for rank, opportunity in enumerate(opportunities, 1):
        evaluation = opportunity.score.model_dump(mode='json')
        win_pre = opportunity.gate['win_pre']
        lines += ['', f'## {rank}. {opportunity.company_name}',
                  f'Score: {opportunity.final_score:g} / 100', '', '営業トリガー',
                  opportunity.candidate.trigger_title,
                  f"Discovery: {', '.join(opportunity.origins)}",
                  f"WIN Pre: {win_pre['win_pre']:g}/10 ({win_pre['confidence']})",
                  f"WIN Unknowns: {', '.join(win_pre.get('unknown_factors', [])) or 'なし'}",
                  '参考順位点（受注確率ではありません）', evaluation['scope_hypothesis']]
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
    return '\n'.join(lines)


def format_error_report(run_id: int, status: str, errors: list[str]) -> str:
    """Create a safe alert without exception bodies or credentials."""
    return '\n'.join([
        '# adely Sales Agent Error', f'Run: {run_id}', f'Status: {status}', '',
        '処理中にエラーが発生しました。',
        *[f'・{error}' for error in errors],
    ])


def split_messages(text: str, limit: int = 1900) -> list[str]:
    """Bound by UTF-16 units too, so astral characters cannot exceed Discord limits."""
    chunks, current, units = [], '', 0
    for character in text:
        size = len(character.encode('utf-16-le')) // 2
        if units + size > limit:
            chunks.append(current)
            current, units = '', 0
        current += character
        units += size
    if current:
        chunks.append(current)
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
