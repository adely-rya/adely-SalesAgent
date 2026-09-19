"""Deterministic hard filters and the low-cost, no-search WIN pre-evaluation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from app.config import Settings
from app.llm import Generation, LLMClient
from app.schemas import CheapWinOutput
from app.v2_discovery import MergedCandidate


@dataclass(frozen=True)
class HardFilterResult:
    excluded: bool
    risk_tags: tuple[str, ...]
    reason: str

    def model_dump(self) -> dict:
        return {'excluded': self.excluded, 'risk_tags': list(self.risk_tags), 'reason': self.reason}


_DIRECT_COMPETITOR_TERMS = (
    '映像制作', '動画制作', '映像プロダクション', '広告代理店', '広告制作', 'creative agency',
    'creative studio', '映像制作会社', 'video production',
)
_LARGE_ENTERPRISE_TERMS = ('東証プライム', 'グローバル本社', 'グループ全体', '従業員数1,000', '従業員数1000')


def hard_filter(candidate: MergedCandidate) -> HardFilterResult:
    """Exclude only direct conflicts; scale and procurement signals remain reviewable risks."""
    text = ' '.join([
        candidate.primary.company_name,
        *(trigger.trigger_title + ' ' + trigger.trigger_summary for trigger in candidate.triggers),
    ]).casefold()
    terms = [term for term in _DIRECT_COMPETITOR_TERMS if term.casefold() in text]
    if terms:
        return HardFilterResult(True, ('direct_competitor_or_agency',),
                                f'営業対象外の可能性が高い業態シグナル: {", ".join(terms[:2])}')
    risks = ['large_enterprise'] if any(term.casefold() in text for term in _LARGE_ENTERPRISE_TERMS) else []
    return HardFilterResult(False, tuple(risks), '明確な対象外シグナルは確認されませんでした')


async def evaluate_cheap_win(client: LLMClient, settings: Settings, candidate: MergedCandidate,
                             vc_profiles: list[dict], filter_result: HardFilterResult) -> Generation[CheapWinOutput]:
    """Use local discovery facts/profile data only.  No web tool is enabled here."""
    input_data = {
        'candidate': candidate.model_dump(), 'vc_profiles': vc_profiles,
        'hard_filter': filter_result.model_dump(),
        'thresholds': {
            'drop_below': settings.win_pre_drop_threshold,
            'diagnostic_at_or_above': settings.win_pre_diagnostic_threshold,
        },
    }
    return await client.generate(model=settings.cheap_win_model, instructions=client.prompt('cheap_win'),
        input_text=json.dumps(input_data, ensure_ascii=False), output_type=CheapWinOutput,
        use_web_search=False, reasoning_effort=settings.cheap_win_reasoning_effort)


def gate_status(result: CheapWinOutput, settings: Settings) -> str:
    if result.hard_blocker or result.win_pre < settings.win_pre_drop_threshold:
        return 'dropped'
    if result.win_pre < settings.win_pre_diagnostic_threshold:
        return 'hold'
    return 'diagnostic'

