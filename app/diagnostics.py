"""Expensive, structured research used only after the cheap WIN gate passes."""
from __future__ import annotations

import json

from app.config import Settings
from app.llm import Generation, LLMClient
from app.schemas import DiagnosticOutput
from app.v2_discovery import MergedCandidate


async def run_diagnostic_research(client: LLMClient, settings: Settings, candidate: MergedCandidate,
                                  win_pre: dict, vc_profiles: list[dict]) -> Generation[DiagnosticOutput]:
    """Research current expression, debt, optional peer gap, and creative lock-in in one bounded call."""
    return await client.generate(model=settings.diagnostic_model, instructions=client.prompt('diagnostic'),
        input_text=json.dumps({
            'candidate': candidate.model_dump(), 'win_pre': win_pre, 'vc_profiles': vc_profiles,
            'peer_research_enabled': settings.peer_research_enabled,
        }, ensure_ascii=False), output_type=DiagnosticOutput, use_web_search=settings.diagnostic_web_search,
        reasoning_effort=settings.diagnostic_reasoning_effort)


def diagnostic_context(value: DiagnosticOutput) -> dict:
    """The precise V2 fields appended to V1 scoring input, preserving the old score contract."""
    return {
        'current_expression': value.current_expression.model_dump(),
        'expression_debt': value.expression_debt.model_dump(),
        'peer_gap': value.peer_gap.model_dump() if value.peer_gap else None,
        'creative_lock_in': value.creative_lock_in.model_dump(),
    }

