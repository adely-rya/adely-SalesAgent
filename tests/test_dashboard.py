"""Offline Control Center tests: only a local SQLite copy, never pipeline APIs."""
from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from fastapi.testclient import TestClient


def make_db(path: Path) -> tuple[str, str]:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE runs(id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, candidate_count INTEGER, scored_count INTEGER, strategy_count INTEGER, error_message TEXT, config_json TEXT);
    CREATE TABLE v3_candidate_pool(id INTEGER PRIMARY KEY,run_id INTEGER,company_id TEXT,company_name TEXT,discovery_origins TEXT,payload_json TEXT,raw_report TEXT,created_at TEXT);
    CREATE TABLE v3_shortlist_decisions(id INTEGER PRIMARY KEY,run_id INTEGER,company_id TEXT,company_name TEXT,selected BOOLEAN,decision_json TEXT,model TEXT,prompt_version TEXT,reasoning_effort TEXT,created_at TEXT);
    CREATE TABLE v3_sales_memos(id INTEGER PRIMARY KEY,run_id INTEGER,company_id TEXT,company_name TEXT,memo_json TEXT,evidence_urls TEXT,warnings TEXT,model TEXT,prompt_version TEXT,reasoning_effort TEXT,created_at TEXT);
    CREATE TABLE v3_final_selections(id INTEGER PRIMARY KEY,run_id INTEGER,company_id TEXT,company_name TEXT,rank INTEGER,selection_json TEXT,model TEXT,prompt_version TEXT,reasoning_effort TEXT,created_at TEXT);
    CREATE TABLE v3_listing_policy_decisions(id INTEGER PRIMARY KEY,run_id INTEGER,candidate_ref TEXT,company_name TEXT,normalized_name TEXT,listing_match_status TEXT,matched_company_name TEXT,security_code TEXT,market_segment TEXT,master_as_of TEXT,policy_action TEXT,phase TEXT,created_at TEXT);
    CREATE TABLE processing_errors(id INTEGER PRIMARY KEY,run_id INTEGER,stage TEXT,subject TEXT,error_type TEXT,exception_type TEXT,error_message TEXT,model TEXT,prompt_version TEXT,created_at TEXT);
    """)
    config='{"summary":{"scout_candidates":68,"listed_companies_excluded":14,"shortlisted":15,"research_success":13,"final_selected":5,"runtime_seconds":1709,"usage_by_stage":{"scout":{"generate_calls":7}},"api_plan":{"web_search_calls_observed":30}}}'
    con.execute("INSERT INTO runs VALUES(1,'2026-09-28','2026-09-28','completed',68,5,0,NULL,?)",(config,))
    payload='{"company_id":"abc123","company_name":"Example Studio","location":"Tokyo","industry":"Technology","official_domain":"example.jp","why_scout_found_it_interesting":"A verified trigger"}'
    con.execute("INSERT INTO v3_candidate_pool VALUES(1,1,'abc123','Example Studio','[\"Web Search\"]',?,'report','2026-09-28')",(payload,))
    memo='{"company_name":"Example Studio","why_now":"Fresh product launch","what_we_learned":["Verified fact"],"current_expression":"Product site","evidence":[{"claim":"Official release","source_url":"https://example.com/release?x=1&y=2"}]}'
    con.execute("INSERT INTO v3_sales_memos VALUES(1,1,'abc123','Example Studio',?,'[\"https://example.com/raw\"]','[]','model','v2',NULL,'2026-09-28')",(memo,))
    selection='{"rank":1,"why_now":"Fresh product launch","proposed_angle":"A precise demo","main_risk":"Budget unknown"}'
    con.execute("INSERT INTO v3_final_selections VALUES(1,1,'abc123','Example Studio',1,?,'model','v2',NULL,'2026-09-28')",(selection,))
    con.commit(); con.close()
    return 'abc123', 'Example Studio'


def test_dashboard_routes_and_crm(tmp_path, monkeypatch):
    path=tmp_path/'sales.db'; company_id,_=make_db(path); monkeypatch.setenv('DASHBOARD_DB_PATH',str(path))
    from app.dashboard.main import app
    with TestClient(app) as client:
        for url in ['/', '/companies', f'/companies/{company_id}', '/runs', '/runs/1', '/analytics', '/system', '/healthz']:
            assert client.get(url).status_code == 200, url
        detail=client.get(f'/companies/{company_id}').text
        assert 'href="https://example.com/release?x=1&amp;y=2"' in detail
        assert client.post(f'/companies/{company_id}/status',data={'status':'prospect'}).status_code==200
        assert client.post(f'/companies/{company_id}/notes',data={'note':'Call after launch'}).status_code==200
        assert client.post(f'/companies/{company_id}/tags',data={'tag':'high priority'}).status_code==200
        assert 'Call after launch' in client.get(f'/companies/{company_id}').text
        assert client.post(f'/companies/{company_id}/tags/delete',data={'tag':'high priority'}).status_code==200


def test_dashboard_uses_existing_db_readonly_if_provided(monkeypatch):
    real=os.getenv('DASHBOARD_REAL_DB')
    if not real or not Path(real).exists(): return
    monkeypatch.setenv('DASHBOARD_DB_PATH',real)
    from app.dashboard import database as db
    rows=db.run_rows()
    assert rows
    detail=db.run_detail(rows[0]['id'])
    assert detail and detail['pool']
    company_id=detail['pool'][0]['company_id']
    assert db.company_detail(company_id)
