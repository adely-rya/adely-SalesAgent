"""Low-volume, robots-aware collection of public fixed sources.

The collector intentionally supports feeds first and only a small, bounded official
news listing for a VC source.  It has no browser automation and is easy to test
with a supplied fetch coroutine.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import hashlib
import logging
import re
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit
from urllib import robotparser
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import SourceEvent, utcnow

log = logging.getLogger(__name__)
USER_AGENT = 'adely-sales-agent/2.0 (+fixed-source-collector)'


@dataclass(frozen=True)
class SourceDefinition:
    source_type: str
    source_name: str
    url: str
    parser: str


DEFAULT_SOURCES = (
    # @Press publishes this feed itself; no page crawling is necessary.
    SourceDefinition('atpress', '@Press', 'https://www.atpress.ne.jp/rss/index.rdf', 'rss'),
    # The official Incubate Fund news page is deliberately bounded to one listing request.
    SourceDefinition('vc_news', 'Incubate Fund', 'https://incubatefund.com/news/', 'incubate_fund_news'),
)


@dataclass(frozen=True)
class ParsedEvent:
    event_type: str
    title: str
    summary: str
    published_at: datetime | None
    source_url: str
    external_id: str
    company_name: str | None = None
    raw_data: dict | None = None


@dataclass
class CollectionResult:
    stored: list[SourceEvent]
    errors: list[str]


def clean_text(value: str | None) -> str:
    value = unescape(value or '')
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', value)).strip()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = clean_text(value)
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    for candidate in (value, value.replace('Z', '+00:00')):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
        except ValueError:
            pass
    for fmt in ('%Y.%m.%d', '%Y/%m/%d', '%Y-%m-%d'):
        try:
            return datetime.strptime(value[:10], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_rss(text: str, source_url: str, event_type: str = 'press_release') -> list[ParsedEvent]:
    """Parse RSS, RDF, or Atom without an additional dependency."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError('Invalid XML feed') from exc
    events: list[ParsedEvent] = []
    items = [node for node in root.iter() if node.tag.rsplit('}', 1)[-1] in {'item', 'entry'}]
    for item in items:
        values: dict[str, str] = {}
        for node in item.iter():
            key = node.tag.rsplit('}', 1)[-1]
            if node.text and key not in values:
                values[key] = node.text
            if key == 'link' and node.attrib.get('href'):
                values['link'] = node.attrib['href']
        title, link = clean_text(values.get('title')), values.get('link', '').strip()
        if not title or not link:
            continue
        link = urljoin(source_url, link)
        identifier = clean_text(values.get('guid') or values.get('id')) or link
        events.append(ParsedEvent(
            event_type=event_type, title=title,
            summary=clean_text(values.get('description') or values.get('summary') or values.get('content')),
            published_at=parse_datetime(values.get('pubDate') or values.get('date') or values.get('published') or values.get('updated')),
            source_url=link, external_id=identifier,
            raw_data={'feed_title': title},
        ))
    return events


class _NewsListingParser(HTMLParser):
    """Small parser for official news listings; only accepts links inside articles."""
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url, self.records, self.current = base_url, [], None
        self.links: list[tuple[str, str]] = []
        self._tag, self._text = '', []
        self._link_href, self._link_text = '', []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == 'a' and attributes.get('href'):
            self._link_href, self._link_text = urljoin(self.base_url, attributes['href']), []
        if tag == 'article':
            self.current = {'title': '', 'url': '', 'date': '', 'category': '', 'summary': ''}
        if self.current is not None:
            self._tag, self._text = tag, []
            if tag == 'a' and attributes.get('href'):
                self.current['_href'] = urljoin(self.base_url, attributes['href'])

    def handle_data(self, data: str) -> None:
        if self._link_href:
            self._link_text.append(data)
        if self.current is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == 'a' and self._link_href:
            title = clean_text(''.join(self._link_text))
            path = urlsplit(self._link_href).path.rstrip('/')
            if title and path.startswith('/news/'):
                self.links.append((title, self._link_href))
            self._link_href, self._link_text = '', []
        if self.current is None:
            return
        value = clean_text(''.join(self._text))
        if tag == 'time':
            self.current['date'] = value
        elif tag == 'a' and self.current.get('_href') and value:
            self.current['title'], self.current['url'] = value, self.current.pop('_href')
        elif tag == 'p' and value and not self.current['summary']:
            self.current['summary'] = value
        elif tag in {'span', 'div'} and value and not self.current['category'] and len(value) < 40:
            self.current['category'] = value
        if tag == 'article':
            if self.current['title'] and self.current['url']:
                self.records.append(self.current)
            self.current = None


def parse_incubate_fund_news(text: str, source_url: str) -> list[ParsedEvent]:
    parser = _NewsListingParser(source_url)
    parser.feed(text)
    events = []
    records = parser.records or [
        {'title': title, 'url': url, 'date': '', 'category': '', 'summary': ''}
        for title, url in dict.fromkeys(parser.links)
    ]
    for record in records:
        title = record['title']
        category = record['category']
        event_type = 'investment' if '投資' in f'{title} {category}' or '出資' in title else 'vc_news'
        events.append(ParsedEvent(event_type=event_type, title=title, summary=record['summary'],
            published_at=parse_datetime(record['date']), source_url=record['url'],
            external_id=record['url'], raw_data={'category': category}))
    return events


def parse_source(source: SourceDefinition, text: str) -> list[ParsedEvent]:
    if source.parser == 'rss':
        return parse_rss(text, source.url)
    if source.parser == 'incubate_fund_news':
        return parse_incubate_fund_news(text, source.url)
    raise ValueError(f'Unknown source parser: {source.parser}')


def save_source_events(session: Session, source: SourceDefinition,
                       events: list[ParsedEvent], limit: int) -> list[SourceEvent]:
    """Idempotently store a bounded batch and return both old and new records."""
    stored: list[SourceEvent] = []
    for event in events[:limit]:
        external_id = event.external_id or hashlib.sha256(event.source_url.encode()).hexdigest()
        existing = session.scalar(select(SourceEvent).where(
            SourceEvent.source_type == source.source_type,
            SourceEvent.source_name == source.source_name,
            SourceEvent.external_id == external_id,
        ))
        if existing is None:
            existing = SourceEvent(source_type=source.source_type, source_name=source.source_name,
                event_type=event.event_type, company_name=event.company_name, title=event.title,
                summary=event.summary, published_at=event.published_at, source_url=event.source_url,
                external_id=external_id, raw_data=event.raw_data or {})
            session.add(existing)
            session.flush()
        stored.append(existing)
    return stored


async def _fetch_text(url: str, timeout: float) -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=timeout, headers={'User-Agent': USER_AGENT}, follow_redirects=True) as client:
        response = await client.get(url)
        return response.status_code, response.text


async def _allowed_by_robots(url: str, timeout: float,
                             fetch: Callable[[str], Awaitable[tuple[int, str]]]) -> bool:
    parts = urlsplit(url)
    robots_url = f'{parts.scheme}://{parts.netloc}/robots.txt'
    try:
        status, robots = await fetch(robots_url)
    except (httpx.HTTPError, asyncio.TimeoutError):
        log.warning('collector skipped url=%s because robots.txt could not be fetched', url)
        return False
    if status == 404:
        return True
    if not 200 <= status < 300:
        return False
    rules = robotparser.RobotFileParser()
    rules.parse(robots.splitlines())
    return rules.can_fetch(USER_AGENT, url)


async def collect_fixed_sources(settings: Settings, factory: sessionmaker[Session], *,
                                sources: tuple[SourceDefinition, ...] = DEFAULT_SOURCES,
                                fetcher: Callable[[str], Awaitable[tuple[int, str]]] | None = None) -> CollectionResult:
    """Collect each configured source once.  Tests inject fetcher; no browser is involved."""
    async def fetch(url: str) -> tuple[int, str]:
        return await _fetch_text(url, settings.collector_timeout_seconds)
    fetch = fetcher or fetch
    all_stored: list[SourceEvent] = []
    errors: list[str] = []
    for source in sources:
        try:
            if not await _allowed_by_robots(source.url, settings.collector_timeout_seconds, fetch):
                raise PermissionError('robots.txt disallows collection')
            status, body = await fetch(source.url)
            if not 200 <= status < 300:
                raise ValueError(f'HTTP {status}')
            parsed = parse_source(source, body)
            with factory.begin() as session:
                all_stored.extend(save_source_events(session, source, parsed,
                                                      settings.collector_max_events_per_source))
            log.info('source collected source=%s events=%d', source.source_name, len(parsed))
        except Exception as exc:
            errors.append(f'{source.source_name}: {type(exc).__name__}')
            log.warning('source collection failed source=%s type=%s', source.source_name, type(exc).__name__)
    return CollectionResult(all_stored, errors)
