"""Extract, verify, merge, and group V2.2 Events."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import json
import logging
import re
from urllib.parse import urlsplit, urlunsplit

from app.config import Settings
from app.deduplication import canonical_url, normalize_name, website_domain
from app.discovery import DISCOVERY_TOPICS
from app.domain import Opportunity, RawItem
from app.llm import Generation, LLMClient
from app.prefilters import PrefilterDecision, extract_raw_item_decisions, route_raw_items
from app.schemas import (Event, EventDiscoveryOutput, EventEvidence, EventInterpretation,
                         FixedEventOutput, WebEventInterpretation)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebEventRejection:
    """A normal, per-Event validation rejection (not a run-level error)."""
    reason: str
    company_name: str | None
    title: str | None
    source_url: str | None
    field: str | None = None
    validation_type: str | None = None


def _log_safe_url(url: str | None) -> str:
    """Keep public URL host/path for diagnostics, omitting query secrets."""
    if not url:
        return ''
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))


def raw_item_payload(raw_item: RawItem, decision: PrefilterDecision) -> dict:
    raw_data = raw_item.raw_data if isinstance(raw_item.raw_data, dict) else {}
    categories = []
    for key in ('category', 'categories', 'category_name', 'source_category'):
        value = raw_data.get(key)
        values = value if isinstance(value, (list, tuple, set)) else [value]
        categories.extend(str(item) for item in values if item and str(item) not in categories)
    return {'id': raw_item.id, 'source_type': raw_item.source_type, 'source_name': raw_item.source_name,
        'company_name': raw_item.company_name, 'title': raw_item.title, 'summary': raw_item.summary,
        'published_at': raw_item.published_at.date().isoformat() if raw_item.published_at else None,
        'source_url': raw_item.source_url, 'source_categories': categories,
        'prefilter_status': decision.status, 'event_type': decision.classified_event_type,
        'event_strength': decision.event_strength, 'prefilter_reason': decision.reason}


async def extract_fixed_events(client: LLMClient, settings: Settings, raw_items: list[RawItem], *,
                               include_hold: bool = True) -> tuple[list[Event], dict[int, PrefilterDecision], set[int], int]:
    """Run source-specific rules and bounded extraction behind one public call."""
    decisions = extract_raw_item_decisions(raw_items, settings)
    dropped_ids = {item.id for item in raw_items if decisions[item.id].status == 'DROP'}
    routed_items = route_raw_items(raw_items, decisions, settings.fixed_discovery_batch_size, include_hold)
    selected_by_source: dict[str, int] = {}
    for item in routed_items:
        selected_by_source[item.source_name] = selected_by_source.get(item.source_name, 0) + 1
    selected_strength = [decisions[item.id].event_strength for item in routed_items]
    log.info('fixed routing selected=%d selected_avg_strength=%.1f sources=%s', len(routed_items),
             sum(selected_strength) / len(selected_strength) if selected_strength else 0.0,
             dict(sorted(selected_by_source.items())))
    if not routed_items:
        return [], decisions, dropped_ids, 0

    routed_by_id = {item.id: item for item in routed_items}
    result: Generation[FixedEventOutput] = await client.generate(
        model=settings.fixed_discovery_model, instructions=client.prompt('fixed_discovery'),
        input_text=json.dumps({'raw_items': [raw_item_payload(item, decisions[item.id]) for item in routed_items],
                               'event_schema': EventInterpretation.model_json_schema()}, ensure_ascii=False),
        output_type=FixedEventOutput, use_web_search=False,
        reasoning_effort=settings.fixed_discovery_reasoning_effort)
    events: list[Event] = []
    rejected = 0
    for extracted in result.value.events:
        if not set(extracted.raw_item_ids) <= routed_by_id.keys():
            rejected += 1
            continue
        source_items = [routed_by_id[item_id] for item_id in extracted.raw_item_ids]
        allowed_urls = {canonical_url(item.source_url) for item in source_items}
        if any(canonical_url(str(fact.source_url)) not in allowed_urls
               for fact in extracted.event.research_facts):
            rejected += 1
            continue
        primary_item = source_items[0]
        evidence = [EventEvidence(source_type=item.source_type, source_name=item.source_name,
            source_url=item.source_url, source_title=item.title, raw_item_id=item.id) for item in source_items]
        rule_strength = max(decisions[item.id].event_strength for item in source_items)
        extracted_strength = extracted.event.strength if extracted.event.strength is not None else rule_strength
        event_data = extracted.event.model_dump()
        event_data.update({'source_type': primary_item.source_type, 'source_name': primary_item.source_name,
            'source_url': primary_item.source_url, 'source_title': primary_item.title,
            'published_at': primary_item.published_at.date() if primary_item.published_at else extracted.event.published_at,
            # Fixed extraction cannot verify a company site beyond the Raw Items.
            'company_website': None,
            'evidence': evidence,
            # The extractor may down-rank the source rule, but cannot inflate it.
            'strength': min(extracted_strength, rule_strength)})
        events.append(Event.model_validate(event_data))
    # All selected Raw Items were interpreted. Dropped items are complete without AI.
    consumed_ids = dropped_ids | set(routed_by_id)
    return events, decisions, consumed_ids, rejected


async def discover_web_events(client: LLMClient, settings: Settings,
                              topics: list[str] | None = None) -> tuple[list[Event], list[WebEventRejection]]:
    """Discover Events and accept only cited, schema-valid evidence."""
    now = datetime.now(timezone.utc)
    events: list[Event] = []
    rejections: list[WebEventRejection] = []
    for topic in topics if topics is not None else DISCOVERY_TOPICS:
        result: Generation[EventDiscoveryOutput] = await client.generate(
            model=settings.discovery_model, instructions=client.prompt('discovery'),
            input_text=json.dumps({'topic': topic, 'today': now.date().isoformat(),
                'preferred_since': (now.date() - timedelta(days=30)).isoformat(),
                'event_schema': WebEventInterpretation.model_json_schema()}, ensure_ascii=False),
            output_type=EventDiscoveryOutput, use_web_search=True,
            reasoning_effort=settings.discovery_reasoning_effort)
        trusted_urls = {canonical_url(url) for url in result.evidence_urls}
        for discovered_event in result.value.events:
            company_name = discovered_event.company_name.strip()
            title = discovered_event.title.strip()
            source_url = str(discovered_event.source_url)
            rejection: WebEventRejection | None = None
            try:
                if not company_name:
                    rejection = WebEventRejection('missing_company_identity', company_name, title,
                                                   source_url, 'company_name', 'empty')
                elif not title or not discovered_event.event_type.strip() or not discovered_event.source_title.strip():
                    invalid_field = ('title' if not title else
                                     'event_type' if not discovered_event.event_type.strip() else 'source_title')
                    rejection = WebEventRejection('invalid_event', company_name, title,
                                                   source_url, invalid_field, 'empty')
                elif canonical_url(source_url) not in trusted_urls:
                    rejection = WebEventRejection('unverified_source_url', company_name, title,
                                                   source_url, 'source_url', 'not_in_search_evidence')
                else:
                    untrusted_fact = next((fact for fact in discovered_event.research_facts
                                           if canonical_url(str(fact.source_url)) not in trusted_urls), None)
                    if untrusted_fact is not None:
                        rejection = WebEventRejection('unverified_fact_url', company_name, title,
                            str(untrusted_fact.source_url), 'research_facts.source_url', 'not_in_search_evidence')
                if discovered_event.company_website and canonical_url(str(discovered_event.company_website)) not in trusted_urls:
                    discovered_event = discovered_event.model_copy(update={'company_website': None})
                if rejection is None and discovered_event.published_at and discovered_event.published_at > now.date():
                    rejection = WebEventRejection('future_published_date', company_name, title,
                        source_url, 'published_at', 'future_date')
                if rejection is None:
                    evidence = [EventEvidence(source_type='web_search', source_name='Web Search',
                        source_url=discovered_event.source_url, source_title=discovered_event.source_title)]
                    event_data = discovered_event.model_dump()
                    event_data.update({'company_name': company_name, 'title': title,
                        'source_type': 'web_search', 'source_name': 'Web Search',
                        'strength': discovered_event.strength if discovered_event.strength is not None else 50,
                        'evidence': evidence})
                    try:
                        events.append(Event.model_validate(event_data))
                    except (ValueError, TypeError) as exc:
                        rejection = WebEventRejection('invalid_event', company_name, title, source_url,
                            None, type(exc).__name__)
            except (ValueError, TypeError) as exc:
                rejection = WebEventRejection('other_validation', company_name or None, title or None,
                    source_url, None, type(exc).__name__)
            if rejection is not None:
                rejections.append(rejection)
                log.info('web event rejected company=%r title=%r reason=%s source_url=%s field=%s validation_type=%s',
                    rejection.company_name, (rejection.title or '')[:180], rejection.reason,
                    _log_safe_url(rejection.source_url), rejection.field or '', rejection.validation_type or '')
    return events, rejections


def merge_events(*event_groups: list[Event]) -> list[Event]:
    """Merge same-source URL duplicates and clear same-title cross-source matches."""
    merged: list[Event] = []
    for event in (item for group in event_groups for item in group):
        company_key = normalize_name(event.company_name)
        title_key = re.sub(r'\W+', '', event.title.casefold())
        event_url = canonical_url(str(event.source_url))
        existing_index = next((index for index, prior in enumerate(merged)
            if normalize_name(prior.company_name) == company_key
            and prior.event_type == event.event_type
            and (canonical_url(str(prior.source_url)) == event_url
                 or re.sub(r'\W+', '', prior.title.casefold()) == title_key)), None)
        if existing_index is None:
            merged.append(event)
            continue
        prior = merged[existing_index]
        evidence_by_key = {(item.source_type, item.source_name, canonical_url(str(item.source_url))): item
                           for item in prior.evidence + event.evidence}
        merged[existing_index] = prior.model_copy(update={'evidence': list(evidence_by_key.values()),
            'strength': max(prior.strength, event.strength),
            'company_website': prior.company_website or event.company_website,
            'location': prior.location or event.location,
            'possible_video_need': prior.possible_video_need or event.possible_video_need,
            'research_facts': prior.research_facts + [fact for fact in event.research_facts
                if fact not in prior.research_facts],
            'research_unknowns': list(dict.fromkeys(prior.research_unknowns + event.research_unknowns))})
    return merged


def group_events_by_company(events: list[Event]) -> list[Opportunity]:
    """Create one Opportunity per clear company identity, preserving every Event."""
    company_groups: list[list[Event]] = []
    for event in events:
        domain = website_domain(str(event.company_website)) if event.company_website else None
        name = normalize_name(event.company_name)
        matched_group = None
        for group in company_groups:
            group_names = {normalize_name(member.company_name) for member in group}
            group_domains = {website_domain(str(member.company_website)) for member in group if member.company_website}
            if (domain and domain in group_domains) or (
                    name in group_names and (not domain or not group_domains or domain in group_domains)):
                matched_group = group
                break
        if matched_group is None:
            company_groups.append([event])
        else:
            matched_group.append(event)

    opportunities: list[Opportunity] = []
    for events_for_company in company_groups:
        # Choose the persistence projection from the best-evidenced Event, not
        # from a model-generated strength estimate alone.
        primary = max(events_for_company,
                      key=lambda event: (len(event.research_facts), len(event.evidence),
                                         len(event.summary), event.published_at or datetime.min.date()))
        candidate = primary.to_candidate()
        source_by_key: dict[tuple[str, str, str, int | None], dict] = {}
        for event in events_for_company:
            evidence_list = event.evidence or [EventEvidence(source_type=event.source_type,
                source_name=event.source_name, source_url=event.source_url, source_title=event.source_title)]
            for evidence in evidence_list:
                key = (evidence.source_type, evidence.source_name, str(evidence.source_url), evidence.raw_item_id)
                source_by_key[key] = {'type': evidence.source_type, 'provider': evidence.source_name,
                    'url': str(evidence.source_url), 'event_id': evidence.raw_item_id}
        opportunities.append(Opportunity(company_name=primary.company_name, events=events_for_company,
            candidate=candidate, origins=sorted({source['type'] for source in source_by_key.values()}),
            sources=list(source_by_key.values())))
    return opportunities
