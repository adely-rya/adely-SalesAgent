"""Fixed-source discovery and conservative merging with the existing web discovery."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.deduplication import canonical_url, normalize_name, website_domain
from app.llm import Generation, LLMClient
from app.models import SourceEvent
from app.schemas import Candidate, FixedDiscoveryOutput


@dataclass(frozen=True)
class CandidateSource:
    type: str
    provider: str
    url: str
    event_id: int | None = None

    def model_dump(self) -> dict:
        return {'type': self.type, 'provider': self.provider, 'url': self.url, 'event_id': self.event_id}


@dataclass(frozen=True)
class CandidateEnvelope:
    candidate: Candidate
    sources: tuple[CandidateSource, ...]

    @property
    def origins(self) -> list[str]:
        return sorted({source.type for source in self.sources})


@dataclass(frozen=True)
class MergedCandidate:
    primary: Candidate
    triggers: tuple[Candidate, ...]
    sources: tuple[CandidateSource, ...]
    discovery_reasons: tuple[str, ...]

    @property
    def origins(self) -> list[str]:
        return sorted({source.type for source in self.sources})

    def model_dump(self) -> dict:
        return {
            'primary': self.primary.model_dump(mode='json'),
            'triggers': [candidate.model_dump(mode='json') for candidate in self.triggers],
            'discovery_origins': self.origins,
            'discovery_sources': [source.model_dump() for source in self.sources],
            'discovery_reasons': list(self.discovery_reasons),
        }


@dataclass(frozen=True)
class FixedDiscoveryResult:
    candidates: tuple[CandidateEnvelope, ...]
    processed_event_ids: tuple[int, ...]


def source_event_payload(event: SourceEvent) -> dict:
    return {
        'id': event.id, 'source_type': event.source_type, 'source_name': event.source_name,
        'event_type': event.event_type, 'company_name': event.company_name, 'title': event.title,
        'summary': event.summary, 'published_at': event.published_at.date().isoformat() if event.published_at else None,
        'source_url': event.source_url,
    }


async def discover_fixed_events(client: LLMClient, settings: Settings,
                                events: list[SourceEvent]) -> FixedDiscoveryResult:
    """Interpret local events only.  This intentionally never enables web search."""
    if not events:
        return FixedDiscoveryResult((), ())
    event_by_id = {event.id: event for event in events}
    result: Generation[FixedDiscoveryOutput] = await client.generate(
        model=settings.fixed_discovery_model, instructions=client.prompt('fixed_discovery'),
        input_text=json.dumps({'source_events': [source_event_payload(event) for event in events]}, ensure_ascii=False),
        output_type=FixedDiscoveryOutput, use_web_search=False,
        reasoning_effort=settings.fixed_discovery_reasoning_effort)
    envelopes: list[CandidateEnvelope] = []
    for item in result.value.candidates:
        selected = [event_by_id[event_id] for event_id in item.source_event_ids if event_id in event_by_id]
        if not selected or len(selected) != len(set(item.source_event_ids)):
            continue
        allowed_urls = {canonical_url(event.source_url) for event in selected}
        # Fixed discovery may not cite a URL that did not come from source_events.
        if canonical_url(str(item.candidate.source_url)) not in allowed_urls:
            continue
        envelopes.append(CandidateEnvelope(item.candidate, tuple(CandidateSource(
            type=event.source_type, provider=event.source_name, url=event.source_url, event_id=event.id)
            for event in selected)))
    return FixedDiscoveryResult(tuple(envelopes), tuple(event_by_id))


def unprocessed_source_events(session: Session, limit: int) -> list[SourceEvent]:
    return list(session.scalars(select(SourceEvent).where(SourceEvent.processed_at.is_(None))
                                .order_by(SourceEvent.published_at.desc(), SourceEvent.id.desc()).limit(limit)))


def web_envelope(candidate: Candidate) -> CandidateEnvelope:
    return CandidateEnvelope(candidate, (CandidateSource(
        type='web_search', provider='OpenAI Web Search', url=str(candidate.source_url)),))


def merge_candidates(candidates: Iterable[CandidateEnvelope]) -> list[MergedCandidate]:
    """Merge only clear identities; preserve every original trigger and provenance."""
    groups: list[list[CandidateEnvelope]] = []
    for envelope in candidates:
        name = normalize_name(envelope.candidate.company_name)
        domain = website_domain(str(envelope.candidate.website)) if envelope.candidate.website else None
        matched = None
        for group in groups:
            names = {normalize_name(item.candidate.company_name) for item in group}
            domains = {website_domain(str(item.candidate.website)) for item in group if item.candidate.website}
            # A name-only item may enrich a domain-known company, but two conflicting domains never merge.
            if domain and domain in domains or (name in names and (not domain or not domains or domain in domains)):
                matched = group
                break
        if matched is None:
            groups.append([envelope])
        else:
            matched.append(envelope)
    merged: list[MergedCandidate] = []
    for envelopes in groups:
        triggers: list[Candidate] = []
        sources: list[CandidateSource] = []
        reasons: list[str] = []
        seen_triggers, seen_sources = set(), set()
        for envelope in envelopes:
            candidate = envelope.candidate
            trigger_key = (candidate.trigger_type, canonical_url(str(candidate.source_url)),
                           normalize_name(candidate.trigger_title))
            if trigger_key not in seen_triggers:
                seen_triggers.add(trigger_key)
                triggers.append(candidate)
            if candidate.trigger_summary not in reasons:
                reasons.append(candidate.trigger_summary)
            for source in envelope.sources:
                source_key = (source.type, source.provider, source.url, source.event_id)
                if source_key not in seen_sources:
                    seen_sources.add(source_key)
                    sources.append(source)
        # Prefer a record with an official site and then richer verified research facts.
        primary = max(triggers, key=lambda item: (item.website is not None, len(item.research_facts)))
        merged.append(MergedCandidate(primary, tuple(triggers), tuple(sources), tuple(reasons)))
    return merged
