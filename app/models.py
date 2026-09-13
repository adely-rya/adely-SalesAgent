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
