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


class EventEvidence(Output):
    source_type: str = Field(min_length=1, max_length=64)
    source_name: str = Field(min_length=1, max_length=128)
    source_url: HttpUrl
    source_title: str = Field(min_length=1, max_length=500)
    raw_item_id: int | None = None


class Event(Output):
    """A verified business change, with its source evidence and company context."""
    company_name: str = Field(min_length=1, max_length=256)
    event_type: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default='', max_length=2000)
    published_at: date | None = None
    source_type: str = Field(min_length=1, max_length=64)
    source_name: str = Field(min_length=1, max_length=128)
    source_url: HttpUrl
    source_title: str = Field(min_length=1, max_length=500)
    evidence: list[EventEvidence] = Field(default_factory=list, max_length=20)
    strength: float = Field(default=50, ge=0, le=100)
    company_website: HttpUrl | None = None
    location: str = ''
    possible_video_need: str = ''
    research_facts: list[ResearchFact] = Field(default_factory=list, max_length=12)
    research_unknowns: list[str] = Field(default_factory=list, max_length=6)

    def to_candidate(self) -> Candidate:
        from datetime import datetime, timezone
        return Candidate(company_name=self.company_name, website=self.company_website,
            trigger_type=self.event_type, trigger_title=self.title, trigger_summary=self.summary,
            published_at=self.published_at, source_url=self.source_url, source_title=self.source_title,
            location=self.location, possible_video_need=self.possible_video_need,
            discovered_at=datetime.now(timezone.utc),
            research_facts=self.research_facts, research_unknowns=self.research_unknowns)


class EventDiscoveryOutput(Output):
    events: list[dict] = Field(max_length=20)


class FixedEventItem(Output):
    event: Event
    raw_item_ids: list[int] = Field(min_length=1, max_length=12)


class FixedEventOutput(Output):
    events: list[FixedEventItem] = Field(max_length=30)


class DiscoveryOutput(Output):
    # Validate candidates individually so one malformed item does not discard its peers.
    candidates: list[dict] = Field(max_length=10)


class CheapWinOutput(Output):
    win_pre: float = Field(ge=0, le=10)
    confidence: Literal['high', 'medium', 'low']
    hard_blocker: bool
    risk_tags: list[str] = Field(max_length=8)
    reason: str = Field(min_length=1, max_length=500)


class ExpressionAsset(Output):
    channel: Literal['corporate_site', 'service_site', 'recruit_site', 'youtube', 'sns', 'brand_film',
                     'corporate_film', 'service_movie', 'recruit_movie', 'other']
    observation: str = Field(min_length=1, max_length=300)
    url: str | None = None
    evidence_confidence: Literal['high', 'medium', 'low']


class CurrentExpression(Output):
    assets: list[ExpressionAsset] = Field(max_length=12)
    summary: str = Field(min_length=1, max_length=500)
    unknowns: list[str] = Field(max_length=6)


class ExpressionDebt(Output):
    score: float = Field(ge=0, le=10)
    business_change: str = Field(min_length=1, max_length=400)
    expression_gap: str = Field(min_length=1, max_length=400)
    reason: str = Field(min_length=1, max_length=500)
    confidence: Literal['high', 'medium', 'low']


class PeerReference(Output):
    name: str = Field(min_length=1, max_length=160)
    comparison: str = Field(min_length=1, max_length=300)
    source_url: str | None = None


class PeerGap(Output):
    score: float = Field(ge=0, le=10)
    peers: list[PeerReference] = Field(max_length=5)
    summary: str = Field(min_length=1, max_length=500)
    confidence: Literal['high', 'medium', 'low']


class CreativeLockIn(Output):
    status: Literal['unknown', 'none_observed', 'possible', 'likely']
    partners_or_credits: list[str] = Field(max_length=8)
    observation: str = Field(min_length=1, max_length=500)
    evidence_confidence: Literal['high', 'medium', 'low']


class DiagnosticOutput(Output):
    current_expression: CurrentExpression
    expression_debt: ExpressionDebt
    peer_gap: PeerGap | None
    creative_lock_in: CreativeLockIn


class VCProfileInput(Output):
    name: str = Field(min_length=1, max_length=128)
    website: str | None = None
    stage_focus: list[str] = Field(default_factory=list)
    sector_focus: list[str] = Field(default_factory=list)
    recruiting_support: bool = False
    sales_support: bool = False
    marketing_support: bool = False
    pr_support: bool = False
    branding_support: bool = False
    creative_support: bool = False
    video_support: bool = False
    creative_support_level: int = Field(default=0, ge=0, le=5)
    potential_partner_score: float | None = Field(default=None, ge=0, le=10)
    notes: str | None = None
    evidence: list[str] = Field(default_factory=list)
    verified_at: datetime | None = None


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
