"""SQLite reader/writer for the Control Center.

This module deliberately uses only stored SQLite data.  It does not import the
pipeline, initialize schedulers, or make any network/API call.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from zoneinfo import ZoneInfo


CRM_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_sales_state (
  company_id VARCHAR(128) PRIMARY KEY,
  status VARCHAR(32) NOT NULL DEFAULT 'unreviewed',
  updated_at DATETIME NOT NULL
);
CREATE TABLE IF NOT EXISTS company_notes (
  id INTEGER PRIMARY KEY,
  company_id VARCHAR(128) NOT NULL,
  note_text TEXT NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);
CREATE TABLE IF NOT EXISTS company_tags (
  id INTEGER PRIMARY KEY,
  company_id VARCHAR(128) NOT NULL,
  tag VARCHAR(64) NOT NULL,
  created_at DATETIME NOT NULL,
  UNIQUE(company_id, tag)
);
CREATE INDEX IF NOT EXISTS ix_company_notes_company_created ON company_notes(company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_company_tags_company ON company_tags(company_id);
CREATE INDEX IF NOT EXISTS ix_company_sales_state_status ON company_sales_state(status);
"""

STATUS_LABELS = {
    'unreviewed': '未確認', 'interesting': '気になる', 'prospect': '営業候補',
    'planned': '連絡予定', 'contacted': '連絡済み', 'replied': '返信あり',
    'meeting': '商談', 'closed_lost': '見送り', 'won': '受注',
}


def db_path() -> Path:
    return Path(os.getenv('DASHBOARD_DB_PATH', 'data/sales.db'))


@contextmanager
def connection(*, write: bool = False) -> Iterator[sqlite3.Connection]:
    path = db_path()
    if not write:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=10)
    else:
        con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        if write:
            con.commit()
    except Exception:
        if write:
            con.rollback()
        raise
    finally:
        con.close()


def migrate_crm() -> None:
    """Add only isolated human-CRM tables; historical AI data is untouched."""
    with connection(write=True) as con:
        con.executescript(CRM_SCHEMA)


def _json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except (TypeError, json.JSONDecodeError):
        return default if default is not None else {}


def _one(con: sqlite3.Connection, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def _all(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def _crm_exists(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_sales_state'").fetchone() is not None


def run_rows(limit: int = 100) -> list[dict[str, Any]]:
    with connection() as con:
        rows = _all(con, 'SELECT * FROM runs ORDER BY id DESC LIMIT ?', (limit,))
        for row in rows:
            row['config'] = _json(row.pop('config_json', {}))
            row['summary'] = row['config'].get('summary', {})
            row['v3_counts'] = row['config'].get('v3_counts', {})
            row['runtime_seconds'] = row['summary'].get('runtime_seconds')
        return rows


def run_detail(run_id: int) -> dict[str, Any] | None:
    with connection() as con:
        run = _one(con, 'SELECT * FROM runs WHERE id=?', (run_id,))
        if not run:
            return None
        run['config'] = _json(run.pop('config_json', {}))
        run['summary'] = run['config'].get('summary', {})
        run['pool'] = _all(con, 'SELECT * FROM v3_candidate_pool WHERE run_id=? ORDER BY id', (run_id,))
        for item in run['pool']:
            item['payload'] = _json(item.pop('payload_json'))
        run['listing'] = _all(con, 'SELECT * FROM v3_listing_policy_decisions WHERE run_id=? ORDER BY id', (run_id,))
        run['shortlist'] = _all(con, 'SELECT * FROM v3_shortlist_decisions WHERE run_id=? ORDER BY id', (run_id,))
        for item in run['shortlist']:
            item['decision'] = _json(item.pop('decision_json'))
        run['memos'] = _all(con, 'SELECT * FROM v3_sales_memos WHERE run_id=? ORDER BY id', (run_id,))
        for item in run['memos']:
            item['memo'] = _json(item.pop('memo_json')); item['evidence_urls'] = _json(item['evidence_urls'], [])
        run['finals'] = _all(con, 'SELECT * FROM v3_final_selections WHERE run_id=? ORDER BY rank', (run_id,))
        for item in run['finals']:
            item['selection'] = _json(item.pop('selection_json'))
        run['errors'] = _all(con, 'SELECT * FROM processing_errors WHERE run_id=? ORDER BY id DESC', (run_id,))
        return run


def company_rows(query: str = '', status: str = '', researched: str = '', selected: str = '',
                 tag: str = '', page: int = 1, page_size: int = 50) -> tuple[list[dict[str, Any]], int]:
    # V3 canonical company_id is intentionally used: it is the only ID shared
    # by V3 pool, shortlist, research and final-selection records.
    clauses, params = ['1=1'], []
    if query:
        clauses.append('c.company_name LIKE ?'); params.append(f'%{query}%')
    if status:
        clauses.append("COALESCE(s.status, 'unreviewed')=?"); params.append(status)
    if tag:
        clauses.append('EXISTS (SELECT 1 FROM company_tags ft WHERE ft.company_id=c.company_id AND ft.tag LIKE ?)'); params.append(f'%{tag}%')
    if researched == 'yes': clauses.append('COALESCE(x.researched,0)>0')
    if researched == 'no': clauses.append('COALESCE(x.researched,0)=0')
    if selected == 'yes': clauses.append('COALESCE(x.selected,0)>0')
    if selected == 'no': clauses.append('COALESCE(x.selected,0)=0')
    where = ' AND '.join(clauses)
    base = """
      FROM (SELECT company_id, MAX(company_name) company_name, MIN(created_at) first_seen,
                   MAX(created_at) last_seen, COUNT(*) discovered,
                   MAX(payload_json) payload_json FROM v3_candidate_pool GROUP BY company_id) c
      LEFT JOIN (SELECT company_id, COUNT(*) researched, (SELECT COUNT(*) FROM v3_final_selections f WHERE f.company_id=m.company_id) selected,
                        (SELECT MIN(rank) FROM v3_final_selections f WHERE f.company_id=m.company_id) best_rank
                 FROM v3_sales_memos m GROUP BY company_id) x ON x.company_id=c.company_id
      LEFT JOIN company_sales_state s ON s.company_id=c.company_id
    """
    select_cols = "SELECT c.*, COALESCE(x.researched,0) researched, COALESCE(x.selected,0) selected, x.best_rank, COALESCE(s.status,'unreviewed') status "
    with connection() as con:
        total = con.execute('SELECT COUNT(*) ' + base + ' WHERE ' + where, tuple(params)).fetchone()[0]
        rows = _all(con, select_cols + base + ' WHERE ' + where + ' ORDER BY c.last_seen DESC LIMIT ? OFFSET ?',
                    tuple(params + [page_size, max(page - 1, 0) * page_size]))
        for row in rows:
            payload = _json(row.pop('payload_json', {}))
            row['location'] = payload.get('location') or 'Unknown'
            row['industry'] = payload.get('industry') or 'Unknown'
            row['tags'] = [x['tag'] for x in _all(con, 'SELECT tag FROM company_tags WHERE company_id=? ORDER BY tag', (row['company_id'],))]
        return rows, total


def company_detail(company_id: str) -> dict[str, Any] | None:
    with connection() as con:
        company = _one(con, "SELECT company_id, MAX(company_name) company_name, MIN(created_at) first_seen, MAX(created_at) last_seen, COUNT(*) discovery_count, MAX(payload_json) payload_json FROM v3_candidate_pool WHERE company_id=? GROUP BY company_id", (company_id,))
        if not company:
            return None
        company['payload'] = _json(company.pop('payload_json'))
        if _crm_exists(con):
            company['state'] = _one(con, 'SELECT * FROM company_sales_state WHERE company_id=?', (company_id,)) or {'status': 'unreviewed'}
            company['notes'] = _all(con, 'SELECT * FROM company_notes WHERE company_id=? ORDER BY created_at DESC', (company_id,))
            company['tags'] = [x['tag'] for x in _all(con, 'SELECT tag FROM company_tags WHERE company_id=? ORDER BY tag', (company_id,))]
        else:
            company['state'], company['notes'], company['tags'] = {'status': 'unreviewed'}, [], []
        company['discoveries'] = _all(con, 'SELECT run_id, created_at, payload_json FROM v3_candidate_pool WHERE company_id=? ORDER BY created_at DESC', (company_id,))
        for item in company['discoveries']: item['payload'] = _json(item.pop('payload_json'))
        company['shortlist'] = _all(con, 'SELECT * FROM v3_shortlist_decisions WHERE company_id=? ORDER BY created_at DESC', (company_id,))
        for item in company['shortlist']: item['decision'] = _json(item.pop('decision_json'))
        company['memos'] = _all(con, 'SELECT * FROM v3_sales_memos WHERE company_id=? ORDER BY created_at DESC', (company_id,))
        for item in company['memos']:
            item['memo'] = _json(item.pop('memo_json')); item['evidence_urls'] = _json(item['evidence_urls'], []); item['warnings'] = _json(item['warnings'], [])
        company['finals'] = _all(con, 'SELECT * FROM v3_final_selections WHERE company_id=? ORDER BY created_at DESC', (company_id,))
        for item in company['finals']: item['selection'] = _json(item.pop('selection_json'))
        company['listing'] = _all(con, 'SELECT * FROM v3_listing_policy_decisions WHERE run_id IN (SELECT run_id FROM v3_candidate_pool WHERE company_id=?) AND company_name=? ORDER BY created_at DESC', (company_id, company['company_name']))
        company['metrics'] = {'research_count': len(company['memos']), 'selection_count': len(company['finals']),
                              'best_rank': min((x['rank'] for x in company['finals']), default=None)}
        return company


def overview() -> dict[str, Any]:
    rows = run_rows(8)
    latest = run_detail(rows[0]['id']) if rows else None
    with connection() as con:
        metrics = _one(con, "SELECT (SELECT COUNT(DISTINCT company_id) FROM v3_candidate_pool) companies, (SELECT COUNT(*) FROM v3_candidate_pool) discoveries, (SELECT COUNT(*) FROM v3_sales_memos) researched, (SELECT COUNT(*) FROM v3_final_selections) selections, (SELECT COUNT(*) FROM (SELECT company_id FROM v3_final_selections GROUP BY company_id HAVING COUNT(*) > 1)) repeats") or {}
    return {'latest': latest, 'runs': rows, 'metrics': metrics}


def analytics() -> dict[str, Any]:
    runs = run_rows(100)
    labels, scout, eligible, research, finals, runtime, api, web = [], [], [], [], [], [], [], []
    for r in reversed(runs):
        s = r['summary']; usage = s.get('usage_by_stage', {})
        labels.append(str(r['started_at'])[:10]); scout.append(s.get('scout_candidates', r['candidate_count']))
        eligible.append(max(0, s.get('scout_candidates', r['candidate_count']) - s.get('listed_companies_excluded', 0)))
        research.append(s.get('research_success', 0)); finals.append(s.get('final_selected', r['scored_count']))
        runtime.append(s.get('runtime_seconds', 0)); api.append(sum(v.get('generate_calls', 0) for v in usage.values() if isinstance(v, dict)))
        web.append(s.get('api_plan', {}).get('web_search_calls_observed', 0))
    with connection() as con:
        locations = _all(con, "SELECT json_extract(payload_json, '$.location') label, COUNT(*) value FROM v3_candidate_pool GROUP BY label ORDER BY value DESC LIMIT 10")
        industries = _all(con, "SELECT json_extract(payload_json, '$.industry') label, COUNT(*) value FROM v3_candidate_pool GROUP BY label ORDER BY value DESC LIMIT 10")
    return {'series': {'labels': labels, 'scout': scout, 'eligible': eligible, 'research': research, 'finals': finals, 'runtime': runtime, 'api': api, 'web': web}, 'locations': locations, 'industries': industries}


def system_snapshot() -> dict[str, Any]:
    # Container-safe, read-only Linux metrics. Every field is independently optional.
    import shutil
    def read(path: str) -> str | None:
        try: return Path(path).read_text().strip()
        except OSError: return None
    mem = {}; raw = read('/proc/meminfo') or ''
    for line in raw.splitlines():
        parts = line.replace(':', '').split()
        if len(parts) >= 2: mem[parts[0]] = int(parts[1]) * 1024
    disk = shutil.disk_usage('/')
    load = None
    try: load = os.getloadavg()
    except OSError: pass
    temp = read('/sys/class/thermal/thermal_zone0/temp')
    path = db_path(); backup = path.parent / 'backups'; logdir = Path(os.getenv('DASHBOARD_LOG_DIR', path.parent))
    size = lambda p: sum(x.stat().st_size for x in p.rglob('*') if x.is_file()) if p.exists() else 0
    log_size = sum(x.stat().st_size for x in logdir.rglob('*.log') if x.is_file()) if logdir.exists() else 0
    return {'load': load, 'temperature_c': (int(temp) / 1000 if temp and temp.isdigit() else None),
            'memory_total': mem.get('MemTotal'), 'memory_used': (mem.get('MemTotal', 0) - mem.get('MemAvailable', 0)) if mem else None,
            'swap_total': mem.get('SwapTotal'), 'swap_used': (mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)) if mem else None,
            'disk_total': disk.total, 'disk_used': disk.used, 'disk_free': disk.free, 'uptime': read('/proc/uptime'),
            'database_size': path.stat().st_size if path.exists() else None, 'backups_size': size(backup), 'logs_size': log_size,
            'database_ok': _database_ok(), 'scheduler': scheduler_info()}


def _database_ok() -> bool:
    try:
        with connection() as con: con.execute('SELECT id FROM runs ORDER BY id DESC LIMIT 1').fetchone()
        return True
    except sqlite3.Error: return False


def scheduler_info() -> dict[str, Any]:
    # The resident scheduler is intentionally isolated in another container; use
    # its shared, configured cron parameters but do not require docker.sock.
    rows = run_rows(1)
    zone_name = os.getenv('TIMEZONE', 'Asia/Tokyo')
    try:
        zone = ZoneInfo(zone_name)
        hour, minute = int(os.getenv('DAILY_RUN_HOUR', '8')), int(os.getenv('DAILY_RUN_MINUTE', '0'))
        now = datetime.now(zone)
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            from datetime import timedelta
            next_run += timedelta(days=1)
        schedule = next_run.isoformat()
    except Exception:
        schedule = 'Not recorded'
    return {'last_run': rows[0]['started_at'] if rows else None, 'next_run': schedule,
            'status': 'Scheduled daily (configuration observed)'}


def set_status(company_id: str, status: str) -> None:
    if status not in STATUS_LABELS: raise ValueError('Invalid sales status')
    with connection(write=True) as con:
        con.execute('INSERT INTO company_sales_state(company_id,status,updated_at) VALUES(?,?,?) ON CONFLICT(company_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at', (company_id, status, datetime.now(timezone.utc).isoformat()))


def add_note(company_id: str, note: str) -> None:
    note = note.strip()
    if not note: raise ValueError('Memo cannot be empty')
    now = datetime.now(timezone.utc).isoformat()
    with connection(write=True) as con: con.execute('INSERT INTO company_notes(company_id,note_text,created_at,updated_at) VALUES(?,?,?,?)', (company_id, note, now, now))


def edit_note(note_id: int, note: str) -> None:
    with connection(write=True) as con: con.execute('UPDATE company_notes SET note_text=?,updated_at=? WHERE id=?', (note.strip(), datetime.now(timezone.utc).isoformat(), note_id))


def add_tag(company_id: str, tag: str) -> None:
    tag = tag.strip()[:64]
    if not tag: raise ValueError('Tag cannot be empty')
    with connection(write=True) as con: con.execute('INSERT OR IGNORE INTO company_tags(company_id,tag,created_at) VALUES(?,?,?)', (company_id, tag, datetime.now(timezone.utc).isoformat()))


def remove_tag(company_id: str, tag: str) -> None:
    with connection(write=True) as con: con.execute('DELETE FROM company_tags WHERE company_id=? AND tag=?', (company_id, tag))
