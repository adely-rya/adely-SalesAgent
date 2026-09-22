"""Portable SQLAlchemy records. Dates are stored in UTC."""
from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, String, Text, JSON, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = 'runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(default='running')
    candidate_count: Mapped[int] = mapped_column(default=0)
    scored_count: Mapped[int] = mapped_column(default=0)
    strategy_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Company(Base):
    __tablename__ = 'companies'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    normalized_name: Mapped[str] = mapped_column(index=True)
    website: Mapped[str | None]
    website_domain: Mapped[str | None] = mapped_column(String(253), unique=True)
    location: Mapped[str]
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Trigger(Base):
    __tablename__ = 'triggers'
    __table_args__ = (UniqueConstraint('company_id', 'fingerprint'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'))
    trigger_type: Mapped[str]
    title: Mapped[str]
    summary: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    source_title: Mapped[str]
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'))
    fingerprint: Mapped[str] = mapped_column(String(64))
    possible_video_need: Mapped[str] = mapped_column(Text)
    model: Mapped[str]
    prompt_version: Mapped[str]
    evidence_urls: Mapped[list] = mapped_column(JSON)


class Score(Base):
    __tablename__ = 'scores'
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'))
    trigger_id: Mapped[int] = mapped_column(ForeignKey('triggers.id'))
    video_need: Mapped[float]
    timing: Mapped[float]
    budget_fit: Mapped[float]
    adely_fit: Mapped[float]
    entry_chance: Mapped[float]
    location_fit: Mapped[float]
    total_score: Mapped[float]
    reason: Mapped[str] = mapped_column(Text)
    risks_json: Mapped[list] = mapped_column(JSON)
    model: Mapped[str]
    prompt_version: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'))
    selected_rank: Mapped[int | None]


class Strategy(Base):
    __tablename__ = 'strategies'
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'))
    trigger_id: Mapped[int] = mapped_column(ForeignKey('triggers.id'))
    content_json: Mapped[dict] = mapped_column(JSON)
    model: Mapped[str]
    prompt_version: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'))
    evidence_urls: Mapped[list] = mapped_column(JSON)


class HumanRating(Base):
    __tablename__ = 'human_ratings'
    __table_args__ = (UniqueConstraint('company_id', 'run_id'),
                     CheckConstraint("rating IN ('good', 'maybe', 'bad')"))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'))
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'))
    rating: Mapped[str]
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessingError(Base):
    __tablename__ = 'processing_errors'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'))
    stage: Mapped[str]
    subject: Mapped[str]
    error_type: Mapped[str]
    exception_type: Mapped[str | None]
    error_message: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str]
    prompt_version: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchScore(Base):
    """Versioned evaluation table; existing legacy scores remain untouched."""
    __tablename__ = 'research_scores'
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'))
    trigger_id: Mapped[int] = mapped_column(ForeignKey('triggers.id'))
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'))
    candidate_json: Mapped[dict] = mapped_column(JSON)
    evaluation_json: Mapped[dict] = mapped_column(JSON)
    total_score: Mapped[float]
    model: Mapped[str]
    prompt_version: Mapped[str]
    selected_rank: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceEvent(Base):
    """A deduplicated, publicly collected event before any model interpretation."""
    __tablename__ = 'source_events'
    __table_args__ = (UniqueConstraint('source_type', 'source_name', 'external_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    source_name: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(64), default='other')
    company_name: Mapped[str | None] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default='')
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(String(256))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict)


class SourceEventPrefilter(Base):
    """Latest deterministic prefilter decision for one collected source event."""
    __tablename__ = 'source_event_prefilters'
    __table_args__ = (
        UniqueConstraint('source_event_id'),
        CheckConstraint("status IN ('PASS', 'HOLD', 'DROP')"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_event_id: Mapped[int] = mapped_column(ForeignKey('source_events.id'), index=True)
    status: Mapped[str] = mapped_column(String(8), index=True)
    reason: Mapped[str] = mapped_column(Text)
    score: Mapped[float]
    rule: Mapped[str] = mapped_column(String(64), index=True)
    classified_event_type: Mapped[str] = mapped_column(String(64))
    # Keep both the legacy score and a stable 0-100 routing strength.  The
    # signal lists make deterministic decisions inspectable without re-running.
    event_strength: Mapped[float] = mapped_column(default=50.0, index=True)
    matched_positive_signals: Mapped[list] = mapped_column(JSON, default=list)
    matched_negative_signals: Mapped[list] = mapped_column(JSON, default=list)
    supporting_signals: Mapped[list] = mapped_column(JSON, default=list)
    prefiltered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VCProfile(Base):
    """Locally curated, relatively stable information about an investor's support model."""
    __tablename__ = 'vc_profiles'
    __table_args__ = (UniqueConstraint('normalized_name'),
                      CheckConstraint('creative_support_level BETWEEN 0 AND 5'))
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    normalized_name: Mapped[str] = mapped_column(String(256), index=True)
    website: Mapped[str | None] = mapped_column(Text)
    stage_focus: Mapped[list] = mapped_column(JSON, default=list)
    sector_focus: Mapped[list] = mapped_column(JSON, default=list)
    recruiting_support: Mapped[bool] = mapped_column(default=False)
    sales_support: Mapped[bool] = mapped_column(default=False)
    marketing_support: Mapped[bool] = mapped_column(default=False)
    pr_support: Mapped[bool] = mapped_column(default=False)
    branding_support: Mapped[bool] = mapped_column(default=False)
    creative_support: Mapped[bool] = mapped_column(default=False)
    video_support: Mapped[bool] = mapped_column(default=False)
    creative_support_level: Mapped[int] = mapped_column(default=0)
    potential_partner_score: Mapped[float | None]
    notes: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CandidateRecord(Base):
    """V2 company opportunity state; it deliberately does not alter V1 Company/Trigger tables."""
    __tablename__ = 'candidates'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey('companies.id'), index=True)
    trigger_id: Mapped[int | None] = mapped_column(ForeignKey('triggers.id'), index=True)
    status: Mapped[str] = mapped_column(String(48), index=True)
    discovery_origins: Mapped[list] = mapped_column(JSON, default=list)
    discovery_sources: Mapped[list] = mapped_column(JSON, default=list)
    candidate_json: Mapped[dict] = mapped_column(JSON)
    merged_json: Mapped[dict] = mapped_column(JSON, default=dict)
    hard_filter_json: Mapped[dict | None] = mapped_column(JSON)
    win_pre_json: Mapped[dict | None] = mapped_column(JSON)
    dropped_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Diagnostic(Base):
    """Costly research output, separated from the low-cost gate for later evaluation."""
    __tablename__ = 'diagnostics'
    __table_args__ = (UniqueConstraint('candidate_id', 'run_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey('candidates.id'), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    trigger_id: Mapped[int] = mapped_column(ForeignKey('triggers.id'), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'), index=True)
    current_expression_json: Mapped[dict] = mapped_column(JSON)
    expression_debt_json: Mapped[dict] = mapped_column(JSON)
    peer_gap_json: Mapped[dict | None] = mapped_column(JSON)
    creative_lock_in_json: Mapped[dict] = mapped_column(JSON)
    evidence_urls: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HumanFeedback(Base):
    """V2 feedback, retaining a reason code in addition to the free-text note."""
    __tablename__ = 'human_feedback'
    __table_args__ = (CheckConstraint("rating IN ('◎', '○', '△', '×')"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'), index=True)
    rating: Mapped[str] = mapped_column(String(1))
    reason: Mapped[str] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
