from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.database import init_database
from app.listing_master import (ListingMatchStatus, ListingMatcher,
                                normalize_company_name, refresh_jpx_master)


def _xlsx_fixture() -> bytes:
    headers = ['日付', 'コード', '銘柄名', '市場・商品区分']
    rows = [
        ['20260930', '6721', 'ウインテスト', 'スタンダード（内国株式）'],
        ['20260930', '6819', '伊豆シャボテンリゾート', 'スタンダード（内国株式）'],
        ['20260930', '1234', '株式会社ABC', 'プライム（内国株式）'],
        ['20260930', '1235', '株式会社ABCD', 'プライム（内国株式）'],
        ['20260930', '2001', '重複', 'グロース（内国株式）'],
        ['20260930', '2002', '重複', 'PRO Market'],
        ['20260930', '1305', 'ABC ETF', 'ETF・ETN'],
    ]

    def cell(row: int, column: int, value: str) -> str:
        ref = f'{chr(ord("A") + column)}{row}'
        escaped = (value.replace('&', '&amp;').replace('<', '&lt;')
                   .replace('>', '&gt;'))
        return f'<c r="{ref}" t="inlineStr"><is><t>{escaped}</t></is></c>'

    xml_rows = []
    for row_number, values in enumerate([headers, *rows], start=1):
        xml_rows.append('<row r="{}">{}</row>'.format(
            row_number, ''.join(cell(row_number, index, value)
                                for index, value in enumerate(values))))
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData>{}</sheetData></worksheet>').format(''.join(xml_rows))
    result = BytesIO()
    with ZipFile(result, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('xl/worksheets/sheet1.xml', sheet)
    return result.getvalue()


def test_company_name_normalization_is_conservative():
    expected = normalize_company_name('ウインテスト')
    for value in ('ウインテスト株式会社', '株式会社ウインテスト',
                  '（株）ウインテスト', '㈱ウインテスト'):
        assert normalize_company_name(value) == expected
    assert normalize_company_name('株式会社ABC') != normalize_company_name('株式会社ABCD')


def test_jpx_import_match_statuses_and_financial_products_are_separate(tmp_path):
    factory = init_database(f'sqlite:///{tmp_path / "listing.db"}')
    status = refresh_jpx_master(factory, data=_xlsx_fixture(), source_url='fixture://jpx')
    assert status['source_as_of'] == '2026-09-30'
    assert status['row_count'] == 7

    matcher = ListingMatcher(factory)
    listed = matcher.match('ウインテスト株式会社', candidate_ref='C001')
    assert listed.status is ListingMatchStatus.CONFIRMED_LISTED
    assert listed.security_code == '6721'
    assert listed.market_segment == 'スタンダード（内国株式）'
    assert listed.policy_action == 'exclude'

    assert matcher.match('株式会社ABC', candidate_ref='C002').status is ListingMatchStatus.CONFIRMED_LISTED
    assert matcher.match('株式会社AB', candidate_ref='C003').status is ListingMatchStatus.NO_EXACT_MATCH
    assert matcher.match('株式会社伊豆シャボテン公園', candidate_ref='C004').status is ListingMatchStatus.NO_EXACT_MATCH
    assert matcher.match('伊豆シャボテンリゾート株式会社', candidate_ref='C005').security_code == '6819'
    assert matcher.match('重複', candidate_ref='C006').status is ListingMatchStatus.AMBIGUOUS
    assert matcher.match('ABC ETF', candidate_ref='C007').status is ListingMatchStatus.NO_EXACT_MATCH
    factory.kw['bind'].dispose()


def test_master_failure_is_atomic_and_unavailable_is_fail_open(tmp_path):
    factory = init_database(f'sqlite:///{tmp_path / "listing.db"}')
    refresh_jpx_master(factory, data=_xlsx_fixture(), source_url='fixture://jpx')
    with pytest.raises(ValueError):
        refresh_jpx_master(factory, data=b'not-an-xlsx', source_url='fixture://bad')
    assert ListingMatcher(factory).match('ウインテスト').status is ListingMatchStatus.CONFIRMED_LISTED
    factory.kw['bind'].dispose()

    empty_factory = init_database(f'sqlite:///{tmp_path / "empty.db"}')
    decision = ListingMatcher(empty_factory).match('株式会社ABC')
    assert decision.status is ListingMatchStatus.MASTER_UNAVAILABLE
    assert decision.policy_action == 'continue'
    empty_factory.kw['bind'].dispose()
