"""Conservative company identity and repeat-trigger detection."""
from datetime import datetime, time, timezone
import hashlib
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Company, Trigger, utcnow
from app.schemas import Candidate


def normalize_name(name: str) -> str:
    name = unicodedata.normalize('NFKC', name).casefold()
    for legal in ('株式会社', '有限会社', '合同会社', '(株)', '(有)', '一般社団法人'):
        name = name.replace(legal, '')
    return re.sub(r'\s+', '', name)


def website_domain(website: str | None) -> str | None:
    if not website:
        return None
    host = (urlsplit(str(website)).hostname or '').lower().rstrip('.')
    return host.removeprefix('www.') or None


def canonical_url(url: str) -> str:
    # Preserve query parameters: they may identify distinct articles.
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))


def persist_candidate(session: Session, candidate: Candidate, run_id: int, model: str,
                      prompt_version: str, evidence_urls: set[str]) -> tuple[Company, Trigger, bool]:
    domain = website_domain(str(candidate.website)) if candidate.website else None
    normalized = normalize_name(candidate.company_name)
    company = session.scalar(select(Company).where(Company.website_domain == domain)) if domain else None
    if company is None:
        matches = list(session.scalars(select(Company).where(Company.normalized_name == normalized)))
        # Do not merge two known, different domains on a common name alone.
        matches = [c for c in matches if not domain or not c.website_domain or c.website_domain == domain]
        if len(matches) == 1:
            company = matches[0]
    if company is None:
        company = Company(name=candidate.company_name, normalized_name=normalized,
            website=str(candidate.website) if candidate.website else None,
            website_domain=domain, location=candidate.location)
        session.add(company)
        session.flush()
    elif domain and not company.website_domain:
        company.website_domain, company.website = domain, str(candidate.website)
    company.last_seen_at = utcnow()
    source = canonical_url(str(candidate.source_url))
    event_key = '|'.join([candidate.trigger_type, normalize_name(candidate.trigger_title),
                          str(candidate.published_at)])
    fingerprint = hashlib.sha256(event_key.encode()).hexdigest()
    existing = session.scalar(select(Trigger).where(Trigger.company_id == company.id,
        Trigger.fingerprint == fingerprint))
    if existing is None:
        # Same article rewritten by the model remains the same trigger.
        existing = session.scalar(select(Trigger).where(Trigger.company_id == company.id,
            Trigger.source_url == source, Trigger.trigger_type == candidate.trigger_type))
    if existing:
        return company, existing, False
    trigger = Trigger(company_id=company.id, trigger_type=candidate.trigger_type,
        title=candidate.trigger_title, summary=candidate.trigger_summary, source_url=source,
        source_title=candidate.source_title,
        published_at=datetime.combine(candidate.published_at, time(), timezone.utc) if candidate.published_at else None,
        discovered_at=candidate.discovered_at or utcnow(), run_id=run_id, fingerprint=fingerprint,
        possible_video_need=candidate.possible_video_need, model=model, prompt_version=prompt_version,
        evidence_urls=sorted(evidence_urls))
    session.add(trigger)
    session.flush()
    return company, trigger, True
