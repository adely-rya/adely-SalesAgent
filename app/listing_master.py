"""JPX listed-company master import and exact-match hard policy.

This module deliberately has no LLM, Web Search, or Discord dependencies. The
only network operation is the explicit JPX official Excel download used by the
``refresh`` command.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from hashlib import sha256
import re
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import init_database
from app.models import (JPXListedCompany, JPXListingMasterMeta,
                        V3CandidatePool, V3ListingPolicyDecision, utcnow)

JPX_PAGE_URL = 'https://www.jpx.co.jp/markets/statistics-equities/misc/01.html'
JPX_XLSX_URL = 'https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx'
_NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
_REQUIRED_HEADERS = ('日付', 'コード', '銘柄名', '市場・商品区分')
_CORPORATE_PREFIX = re.compile(r'^(?:株式会社|\(株\)|（株）)\s*')
_CORPORATE_SUFFIX = re.compile(r'\s*(?:株式会社|\(株\)|（株）)$')


class ListingMatchStatus(StrEnum):
    CONFIRMED_LISTED = 'CONFIRMED_LISTED'
    NO_EXACT_MATCH = 'NO_EXACT_MATCH'
    AMBIGUOUS = 'AMBIGUOUS'
    MASTER_UNAVAILABLE = 'MASTER_UNAVAILABLE'


@dataclass(frozen=True)
class JPXImportRow:
    security_code: str
    company_name: str
    normalized_name: str
    market_segment: str
    security_type: str
    source_as_of: date


@dataclass(frozen=True)
class JPXImport:
    rows: list[JPXImportRow]
    source_as_of: date
    source_sha256: str
    source_url: str


@dataclass(frozen=True)
class ListingMatch:
    candidate_ref: str
    company_name: str
    normalized_name: str
    status: ListingMatchStatus
    matched_company_name: str | None
    security_code: str | None
    market_segment: str | None
    master_as_of: date | None
    policy_action: str
    phase: str = 'pre_allocation'

    @property
    def is_excluded(self) -> bool:
        return self.status is ListingMatchStatus.CONFIRMED_LISTED


def normalize_company_name(value: str) -> str:
    """Normalize only harmless representation differences, never fuzzy-match."""
    import unicodedata
    value = unicodedata.normalize('NFKC', value or '').strip()
    value = re.sub(r'\s+', ' ', value).strip()
    value = _CORPORATE_PREFIX.sub('', value)
    value = _CORPORATE_SUFFIX.sub('', value)
    return value.strip().casefold()


def _security_type(market_segment: str) -> str:
    if any(token in market_segment for token in ('ETF', 'ETN', 'REIT', 'ファンド', 'インフラ')):
        return 'financial_product'
    if '出資証券' in market_segment:
        return 'other_security'
    if '株式' in market_segment or market_segment == 'PRO Market':
        return 'equity'
    return 'other_security'


def _column_index(cell_ref: str) -> int:
    match = re.match(r'([A-Z]+)', cell_ref or '')
    if not match:
        return -1
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - ord('A') + 1
    return index - 1


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    value = cell.find('m:v', _NS)
    if value is None:
        inline = cell.find('m:is', _NS)
        return ''.join(item.text or '' for item in inline.iter('{%s}t' % _NS['m'])) if inline is not None else ''
    text = value.text or ''
    if cell.get('t') == 's':
        return shared_strings[int(text)]
    return text


def parse_jpx_xlsx(data: bytes, *, source_url: str = JPX_XLSX_URL) -> JPXImport:
    """Parse and validate the official JPX workbook using only the stdlib."""
    try:
        archive = ZipFile(__import__('io').BytesIO(data))
        with archive:
            shared_strings: list[str] = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
                shared_strings = [
                    ''.join(item.text or '' for item in si.iter('{%s}t' % _NS['m']))
                    for si in root.findall('m:si', _NS)
                ]
            sheet_name = next((name for name in archive.namelist()
                               if name.startswith('xl/worksheets/sheet') and name.endswith('.xml')), None)
            if sheet_name is None:
                raise ValueError('JPX workbook has no worksheet')
            root = ET.fromstring(archive.read(sheet_name))
    except (BadZipFile, KeyError, ET.ParseError, ValueError, IndexError, OSError, RuntimeError) as exc:
        raise ValueError(f'Invalid JPX workbook: {type(exc).__name__}') from exc

    rows = root.findall('.//m:sheetData/m:row', _NS)
    if not rows:
        raise ValueError('JPX workbook has no rows')

    header_cells = {_column_index(cell.get('r', '')): _cell_value(cell, shared_strings)
                    for cell in rows[0].findall('m:c', _NS)}
    header_indexes = {header: index for index, header in header_cells.items()}
    missing = [header for header in _REQUIRED_HEADERS if header not in header_indexes]
    if missing:
        raise ValueError(f'JPX workbook missing headers: {missing}')

    imported_rows: list[JPXImportRow] = []
    source_dates: set[date] = set()
    for row in rows[1:]:
        values = {_column_index(cell.get('r', '')): _cell_value(cell, shared_strings)
                  for cell in row.findall('m:c', _NS)}
        raw_date = values.get(header_indexes['日付'], '').strip()
        code = values.get(header_indexes['コード'], '').strip().upper()
        company_name = values.get(header_indexes['銘柄名'], '').strip()
        market_segment = values.get(header_indexes['市場・商品区分'], '').strip()
        if not raw_date and not code and not company_name and not market_segment:
            continue
        if not raw_date or not company_name or not market_segment:
            raise ValueError('JPX workbook contains an incomplete listing row')
        try:
            source_as_of = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]))
        except (ValueError, IndexError) as exc:
            raise ValueError(f'Invalid JPX source date: {raw_date}') from exc
        source_dates.add(source_as_of)
        imported_rows.append(JPXImportRow(
            security_code=code, company_name=company_name,
            normalized_name=normalize_company_name(company_name),
            market_segment=market_segment, security_type=_security_type(market_segment),
            source_as_of=source_as_of))

    if not imported_rows:
        raise ValueError('JPX workbook contains no listing rows')
    if len(source_dates) != 1:
        raise ValueError(f'JPX workbook has inconsistent source dates: {sorted(source_dates)}')
    return JPXImport(rows=imported_rows, source_as_of=source_dates.pop(),
                     source_sha256=sha256(data).hexdigest(), source_url=source_url)


def download_jpx_xlsx(url: str = JPX_XLSX_URL, timeout: float = 60) -> bytes:
    request = Request(url, headers={'User-Agent': 'adely-sales-agent JPX master importer'})
    with urlopen(request, timeout=timeout) as response:
        data = response.read()
    if not data:
        raise ValueError('JPX download was empty')
    return data


def refresh_jpx_master(factory: sessionmaker, *, data: bytes | None = None,
                       source_url: str = JPX_XLSX_URL) -> dict:
    """Validate first, then replace the current master in one transaction."""
    raw = data if data is not None else download_jpx_xlsx(source_url)
    imported = parse_jpx_xlsx(raw, source_url=source_url)
    imported_at = utcnow()
    with factory.begin() as session:
        session.execute(delete(JPXListedCompany))
        session.execute(delete(JPXListingMasterMeta))
        session.add_all(JPXListedCompany(
            security_code=row.security_code, company_name=row.company_name,
            normalized_name=row.normalized_name, market_segment=row.market_segment,
            security_type=row.security_type, source_as_of=row.source_as_of,
            imported_at=imported_at, source_url=imported.source_url)
            for row in imported.rows)
        session.add(JPXListingMasterMeta(
            source_as_of=imported.source_as_of, downloaded_at=imported_at,
            source_url=imported.source_url, source_sha256=imported.source_sha256,
            row_count=len(imported.rows)))
    return master_status(factory)


def master_status(factory: sessionmaker) -> dict:
    with factory() as session:
        meta = session.scalar(select(JPXListingMasterMeta).order_by(JPXListingMasterMeta.id.desc()))
        counts = Counter(session.scalars(select(JPXListedCompany.market_segment)).all())
    return {
        'source_as_of': meta.source_as_of.isoformat() if meta else None,
        'downloaded_at': meta.downloaded_at.isoformat() if meta else None,
        'source_url': meta.source_url if meta else None,
        'source_sha256': meta.source_sha256 if meta else None,
        'row_count': meta.row_count if meta else 0,
        'market_distribution': dict(counts),
    }


class ListingMatcher:
    """Exact, fail-open matcher over one immutable in-memory master snapshot."""
    def __init__(self, factory: sessionmaker) -> None:
        with factory() as session:
            self.meta = session.scalar(select(JPXListingMasterMeta).order_by(JPXListingMasterMeta.id.desc()))
            rows = list(session.scalars(select(JPXListedCompany)))
        self.available = self.meta is not None and bool(rows)
        self._by_code: dict[str, list[JPXListedCompany]] = defaultdict(list)
        self._by_name: dict[str, list[JPXListedCompany]] = defaultdict(list)
        for row in rows:
            if row.security_type == 'equity':
                self._by_code[row.security_code.upper()].append(row)
                self._by_name[row.normalized_name].append(row)

    def match(self, company_name: str, *, candidate_ref: str = '',
              security_code: str | None = None, phase: str = 'pre_allocation') -> ListingMatch:
        normalized = normalize_company_name(company_name)
        master_as_of = self.meta.source_as_of if self.meta else None
        base = dict(candidate_ref=candidate_ref, company_name=company_name,
                    normalized_name=normalized, master_as_of=master_as_of, phase=phase)
        if not self.available:
            return ListingMatch(status=ListingMatchStatus.MASTER_UNAVAILABLE,
                                matched_company_name=None, security_code=None,
                                market_segment=None, policy_action='continue', **base)

        code_matches = self._by_code.get((security_code or '').strip().upper(), []) if security_code else []
        if len(code_matches) == 1:
            row = code_matches[0]
            return ListingMatch(status=ListingMatchStatus.CONFIRMED_LISTED,
                                matched_company_name=row.company_name, security_code=row.security_code,
                                market_segment=row.market_segment, policy_action='exclude', **base)
        if len(code_matches) > 1:
            return ListingMatch(status=ListingMatchStatus.AMBIGUOUS,
                                matched_company_name=None, security_code=None,
                                market_segment=None, policy_action='continue', **base)

        name_matches = self._by_name.get(normalized, [])
        if len(name_matches) == 1:
            row = name_matches[0]
            return ListingMatch(status=ListingMatchStatus.CONFIRMED_LISTED,
                                matched_company_name=row.company_name, security_code=row.security_code,
                                market_segment=row.market_segment, policy_action='exclude', **base)
        if len(name_matches) > 1:
            return ListingMatch(status=ListingMatchStatus.AMBIGUOUS,
                                matched_company_name=None, security_code=None,
                                market_segment=None, policy_action='continue', **base)
        return ListingMatch(status=ListingMatchStatus.NO_EXACT_MATCH,
                            matched_company_name=None, security_code=None,
                            market_segment=None, policy_action='continue', **base)


def save_listing_decisions(factory: sessionmaker, run_id: int,
                           decisions: Iterable[ListingMatch]) -> None:
    with factory.begin() as session:
        for decision in decisions:
            session.add(V3ListingPolicyDecision(
                run_id=run_id, candidate_ref=decision.candidate_ref,
                company_name=decision.company_name, normalized_name=decision.normalized_name,
                listing_match_status=decision.status.value,
                matched_company_name=decision.matched_company_name,
                security_code=decision.security_code, market_segment=decision.market_segment,
                master_as_of=decision.master_as_of, policy_action=decision.policy_action,
                phase=decision.phase))


def replay_run(factory: sessionmaker, run_id: int) -> list[ListingMatch]:
    matcher = ListingMatcher(factory)
    with factory() as session:
        pool = list(session.scalars(select(V3CandidatePool)
                                    .where(V3CandidatePool.run_id == run_id)
                                    .order_by(V3CandidatePool.id)))
    decisions = [matcher.match(item.company_name,
                               candidate_ref=(item.payload_json or {}).get('candidate_ref', f'ROW{item.id}'),
                               phase='offline_replay')
                 for item in pool]
    if decisions:
        save_listing_decisions(factory, run_id, decisions)
    return decisions


def _print_replay(decisions: list[ListingMatch]) -> None:
    counts = Counter(decision.status.value for decision in decisions)
    print(f'Total candidates: {len(decisions)}')
    for status in ListingMatchStatus:
        print(f'{status.value}: {counts.get(status.value, 0)}')
    print(f'Research Allocation candidates: {sum(not item.is_excluded for item in decisions)}')
    print('CONFIRMED_LISTED exclusions')
    for item in decisions:
        if item.is_excluded:
            print(f'- {item.candidate_ref}\t{item.company_name}\t{item.matched_company_name}'
                  f'\t{item.security_code}\t{item.market_segment}')


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description='JPX listed-company master tools')
    parser.add_argument('command', choices=['refresh', 'status', 'replay'])
    parser.add_argument('--run-id', type=int, help='replay: stored V3 run ID')
    args = parser.parse_args()
    settings = Settings.from_env()
    factory = init_database(settings.database_url)
    try:
        if args.command == 'refresh':
            result = refresh_jpx_master(factory)
            print(f"source_as_of={result['source_as_of']} row_count={result['row_count']}")
            for key, count in sorted(result['market_distribution'].items()):
                print(f'market={key}\tcount={count}')
            return 0
        if args.command == 'status':
            result = master_status(factory)
            for key, value in result.items():
                print(f'{key}={value}')
            return 0
        if args.run_id is None:
            parser.error('replay requires --run-id')
        _print_replay(replay_run(factory, args.run_id))
        return 0
    finally:
        factory.kw['bind'].dispose()


if __name__ == '__main__':
    raise SystemExit(main())
