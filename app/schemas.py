from datetime import date, datetime
import math
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class Output(BaseModel):
    model_config = ConfigDict(extra='forbid')


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
