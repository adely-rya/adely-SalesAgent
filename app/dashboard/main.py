from __future__ import annotations

import json
from pathlib import Path
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.dashboard import database as db

ROOT = Path(__file__).parent
app = FastAPI(title='Sales Agent Control Center', docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
templates = Jinja2Templates(directory=ROOT / 'templates')
templates.env.filters['json'] = lambda value: json.dumps(value, ensure_ascii=False)
# Older partial records may explicitly contain JSON null arrays.  Render them
# as an empty list instead of turning an otherwise useful detail page into 500.
templates.env.filters['join'] = lambda value, separator='': separator.join(str(x) for x in (value or []))
templates.env.globals['status_labels'] = db.STATUS_LABELS


@app.on_event('startup')
def startup() -> None:
    db.migrate_crm()


def render(request: Request, name: str, **context: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {'nav': request.url.path, **context})


@app.get('/healthz')
def healthz() -> dict[str, str]:
    return {'status': 'ok' if db._database_ok() else 'degraded'}


@app.get('/', response_class=HTMLResponse)
def overview(request: Request) -> HTMLResponse:
    return render(request, 'overview.html', data=db.overview())


@app.get('/companies', response_class=HTMLResponse)
def companies(request: Request, q: str = '', status: str = '', researched: str = '', selected: str = '', tag: str = '', page: int = Query(1, ge=1)) -> HTMLResponse:
    rows, total = db.company_rows(q, status, researched, selected, tag, page)
    return render(request, 'companies.html', rows=rows, total=total, page=page,
                  filters={'q': q, 'status': status, 'researched': researched, 'selected': selected, 'tag': tag})


@app.get('/companies/{company_id}', response_class=HTMLResponse)
def company(request: Request, company_id: str) -> HTMLResponse:
    data = db.company_detail(company_id)
    if not data: raise HTTPException(404, 'Company not found')
    return render(request, 'company.html', company=data)


@app.post('/companies/{company_id}/status')
def update_status(company_id: str, status: str = Form(...)) -> RedirectResponse:
    db.set_status(company_id, status)
    return RedirectResponse(f'/companies/{company_id}', 303)


@app.post('/companies/{company_id}/notes')
def create_note(company_id: str, note: str = Form(...)) -> RedirectResponse:
    db.add_note(company_id, note)
    return RedirectResponse(f'/companies/{company_id}#notes', 303)


@app.post('/notes/{note_id}')
def update_note(note_id: int, company_id: str = Form(...), note: str = Form(...)) -> RedirectResponse:
    db.edit_note(note_id, note)
    return RedirectResponse(f'/companies/{company_id}#notes', 303)


@app.post('/companies/{company_id}/tags')
def create_tag(company_id: str, tag: str = Form(...)) -> RedirectResponse:
    db.add_tag(company_id, tag)
    return RedirectResponse(f'/companies/{company_id}', 303)


@app.post('/companies/{company_id}/tags/delete')
def delete_tag(company_id: str, tag: str = Form(...)) -> RedirectResponse:
    db.remove_tag(company_id, tag)
    return RedirectResponse(f'/companies/{company_id}', 303)


@app.get('/runs', response_class=HTMLResponse)
def runs(request: Request) -> HTMLResponse:
    return render(request, 'runs.html', rows=db.run_rows())


@app.get('/runs/{run_id}', response_class=HTMLResponse)
def run(request: Request, run_id: int) -> HTMLResponse:
    data = db.run_detail(run_id)
    if not data: raise HTTPException(404, 'Run not found')
    return render(request, 'run.html', run=data)


@app.get('/analytics', response_class=HTMLResponse)
def analytics(request: Request) -> HTMLResponse:
    return render(request, 'analytics.html', data=db.analytics())


@app.get('/system', response_class=HTMLResponse)
def system(request: Request) -> HTMLResponse:
    return render(request, 'system.html', system=db.system_snapshot())
