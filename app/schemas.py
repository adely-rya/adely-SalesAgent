from datetime import date, datetime
from typing import Annotated, Literal
import math
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class Output(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ResearchFact(Output):
    topic: Literal['company_scale', 'change', 'timing', 'existing_expression', 'sns', 'production_setup']
    fact: str = Field(min_length=1, max_length=400)
    source_url: HttpUrl


class Candidate(Output):
    company_name: str = Field(min_length=1)
    website: HttpUrl | None
    trigger_type: str = Field(min_length=1)
    trigger_title: str = Field(min_length=1)
    trigger_summary: str
    published_at: date | None
    source_url: HttpUrl
    source_title: str
    location: str
    possible_video_need: str
    discovered_at: datetime | None = None
    research_facts: list[ResearchFact] = Field(default_factory=list, max_length=12)
    research_unknowns: list[str] = Field(default_factory=list, max_length=6)


class DiscoveryOutput(Output):
    # Validate candidates individually so one malformed item does not discard its peers.
    candidates: list[dict] = Field(max_length=10)


DIMENSIONS = ('video_need', 'timing', 'budget_fit', 'adely_fit', 'entry_chance', 'location_fit')


def clamp(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Score must be finite')
    return max(0.0, min(10.0, value))


class ScoreOutput(Output):
    video_need: float
    timing: float
    budget_fit: float
    adely_fit: float
    entry_chance: float
    location_fit: float
    reason: str
    risks: list[str]

    @field_validator(*DIMENSIONS)
    @classmethod
    def clamp_score(cls, value: float) -> float:
        return clamp(value)


class Proposal(Output):
    title: str
    description: str
    deliverables: list[str]


class StrategyOutput(Output):
    why_now: str
    business_context: str
    video_problem_hypothesis: str
    proposal: Proposal
    estimated_budget: str
    target_department: str
    target_role: str
    first_contact_method: str
    sales_angle: str
    risks: list[str]
    research_notes: list[str]


Rating = Annotated[int, Field(strict=True, ge=0, le=10)]


class NeedScores(Output):
    identity_shift: Rating
    narrative_strength: Rating
    communication_moment: Rating
    expression_gap: Rating
    visual_story_potential: Rating


class WinScores(Output):
    budget_likelihood: Rating
    procurement_access: Rating
    creative_investment: Rating
    competitive_openness: Rating
    proposal_fit: Rating


class DeliverScores(Output):
    production_scale_fit: Rating
    capability_fit: Rating
    quality_bar_fit: Rating
    logistics_fit: Rating
    operational_complexity_fit: Rating


class StageEvidence(Output):
    coverage: Literal['high', 'medium', 'low']
    reason: str = Field(min_length=1, max_length=400)


class EvidenceCoverage(Output):
    need: StageEvidence
    win: StageEvidence
    deliver: StageEvidence


class EvaluationOutput(Output):
    need: NeedScores
    win: WinScores
    deliver: DeliverScores
    scope_hypothesis: str = Field(min_length=1, max_length=300)
    evidence_coverage: EvidenceCoverage
    reason: str = Field(min_length=1, max_length=600)
    strongest_signals: list[str] = Field(max_length=3)
    risks: list[str] = Field(max_length=3)
    research_needed: list[str] = Field(max_length=2)
