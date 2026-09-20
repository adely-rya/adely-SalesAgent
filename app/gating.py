"""Deterministic hard filters and the low-cost, no-search WIN pre-evaluation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from app.config import Settings
from app.llm import Generation, LLMClient
from app.domain import Opportunity
from app.schemas import CheapWinOutput


@dataclass(frozen=True)
class HardFilterResult:
    excluded: bool
    risk_tags: tuple[str, ...]
    reason: str

    def model_dump(self) -> dict:
        return {'excluded': self.excluded, 'risk_tags': list(self.risk_tags), 'reason': self.reason}


def hard_filter(candidate: Opportunity) -> HardFilterResult:
    """Only reject from structured company identity facts, never incidental event text.

    The current inputs do not carry a verified, structured company business
    type.  Therefore this deterministic stage must abstain and leave the
    decision to Cheap WIN, which receives source-scoped evidence explicitly.
    """
    del candidate
    return HardFilterResult(False, (),
        '会社自体の業態を示す構造化された確認事実がないため、自動除外せずCheap WINへ渡します')


async def evaluate_cheap_win(client: LLMClient, settings: Settings, candidate: Opportunity,
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
    if result.hard_blocker:
        return 'drop'
    # Low confidence is an information gap, not negative evidence. Preserve
    # high- and low-scoring uncertain Opportunities for later review.
    if result.confidence == 'low':
        return 'hold'
    if result.win_pre < settings.win_pre_drop_threshold and result.confidence == 'high':
        return 'drop'
    if result.win_pre < settings.win_pre_diagnostic_threshold:
        return 'hold'
    return 'research'
