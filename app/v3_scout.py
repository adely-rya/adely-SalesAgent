"""Cheap Scout records and the raw daily report consumed by V3 LLM stages."""
from __future__ import annotations

from collections import Counter
import re

from app.domain import Opportunity


def display_origin(opportunity: Opportunity) -> str:
    origins = set(opportunity.origins or [])
    labels = []
    if 'fixed' in origins or any(origin != 'web_search' for origin in origins):
        labels.append('Fixed Source')
    if 'web_search' in origins:
        labels.append('Web Search')
    return ' + '.join(labels) or 'Unknown'


def _source_lines(opportunity: Opportunity, limit: int = 5) -> list[str]:
    seen: set[str] = set()
    lines: list[str] = []
    for event in opportunity.events:
        url = str(event.source_url)
        if url not in seen:
            seen.add(url)
            lines.append(f'- {event.source_title}: {url}')
    for source in opportunity.sources:
        url = str(source.get('url', ''))
        if url and url not in seen:
            seen.add(url)
            lines.append(f'- {source.get("provider", "Source")}: {url}')
        if len(lines) >= limit:
            break
    return lines[:limit]


def _company_type(opportunity: Opportunity) -> str:
    text = ' '.join([opportunity.company_name, opportunity.candidate.possible_video_need,
                     *(event.title + ' ' + event.summary for event in opportunity.events)])
    if re.search(r'製造|メーカー|工場|製品|食品|繊維', text, re.I):
        return 'Manufacturing / Product'
    if re.search(r'建築|建設|不動産|設備|工事|住宅', text, re.I):
        return 'Construction / Real estate'
    if re.search(r'採用|リクルート|社員', text, re.I):
        return 'Recruitment / Corporate change'
    if re.search(r'AI|SaaS|IT|ソフト|システム|データ', text, re.I):
        return 'Technology / SaaS'
    if re.search(r'店舗|地域|ショールーム|ホテル|飲食', text, re.I):
        return 'Regional / Consumer service'
    return 'Other'


def scout_record(opportunity: Opportunity) -> dict:
    facts = []
    for event in opportunity.events:
        facts.extend(fact.fact for fact in event.research_facts)
    changes = [event.title for event in opportunity.events if event.title]
    return {
        'company_id': opportunity.opportunity_id,
        'company_name': opportunity.company_name,
        'discovery_source': display_origin(opportunity),
        'official_domain': str(opportunity.candidate.website or ''),
        'location': opportunity.candidate.location,
        'industry': _company_type(opportunity),
        'known_company_profile': opportunity.candidate.trigger_summary,
        'recent_signals': changes,
        'what_changed': changes,
        'when_it_changed': [str(event.published_at) for event in opportunity.events if event.published_at],
        'cheap_observations': facts,
        'known_current_expression': [],
        'known_video_assets': [],
        'known_company_size': None,
        'known_listed_status': None,
        'why_scout_found_it_interesting': opportunity.candidate.possible_video_need,
        'source_urls': [str(event.source_url) for event in opportunity.events],
    }


def build_scout_report(opportunities: list[Opportunity]) -> tuple[str, list[dict]]:
    """Render a fact-forward notebook; omit routine unknowns."""
    records = [scout_record(opportunity) for opportunity in opportunities]
    sections = []
    for opportunity, record in zip(opportunities, records):
        lines = [f'## {record["company_name"]}', '', 'Found via',
                 f'- {record["discovery_source"]}', '', 'Company']
        if record['location']:
            lines.append(f'- {record["location"]}')
        lines.append(f'- {record["industry"]}')
        lines += ['', 'Recent change']
        lines.extend(f'- {item}' for item in record['recent_signals'][:5])
        lines += ['', 'Observed expression']
        lines.append('- No detailed expression observation yet; verify in Deep Research.')
        lines += ['', 'Scout notes']
        lines.extend(f'- Observed: {item}' for item in record['cheap_observations'][:4])
        if record['why_scout_found_it_interesting']:
            lines.append(f'- Hypothesis to test: {record["why_scout_found_it_interesting"]}')
        lines += ['', 'Sources']
        lines.extend(_source_lines(opportunity))
        sections.append('\n'.join(lines))
    return '\n\n'.join(sections), records


def scout_distribution(records: list[dict]) -> dict[str, int]:
    return dict(Counter(record['industry'] for record in records))
