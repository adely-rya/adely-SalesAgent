"""Small Python objects passed between the V2.2 pipeline stages.

The ORM remains an implementation detail of the persistence adapters.  V1's
Candidate schema is still used at the V1 boundary; V2.2 models discoveries as
Events and groups those into company-level Opportunities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.schemas import Candidate, DiagnosticOutput, EvaluationOutput, Event


@dataclass(frozen=True)
class RawItem:
    id: int
    source_type: str
    source_name: str
    title: str
    summary: str
    company_name: str | None
    published_at: datetime | None
    source_url: str
    event_type: str
    raw_data: dict[str, Any]


@dataclass
class Opportunity:
    company_name: str
    events: list[Event]
    candidate: Candidate
    origins: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    status: str = 'pending'
    gate: dict[str, Any] | None = None
    research: DiagnosticOutput | None = None
    research_evidence_urls: list[str] = field(default_factory=list)
    score: EvaluationOutput | None = None
    final_score: float | None = None
    strategy: dict[str, Any] | None = None

    @property
    def opportunity_id(self) -> str:
        # Stable within a run and useful to a human trace command before the
        # database assigns a CandidateRecord integer key.
        import hashlib
        import re
        identity = re.sub(r'\s+', '', self.company_name.casefold())
        return hashlib.sha256(identity.encode()).hexdigest()[:12]

    @property
    def published_at(self) -> date | None:
        return self.candidate.published_at

    def model_dump(self) -> dict[str, Any]:
        return {
            'opportunity_id': self.opportunity_id,
            'company_name': self.company_name,
            'events': [event.model_dump(mode='json') for event in self.events],
            'origins': self.origins,
            'sources': self.sources,
            'gate': self.gate,
            'research': self.research.model_dump(mode='json') if self.research else None,
            'research_evidence_urls': self.research_evidence_urls,
            'score': self.score.model_dump(mode='json') if self.score else None,
            'final_score': self.final_score,
            'status': self.status,
            'strategy': self.strategy,
        }
