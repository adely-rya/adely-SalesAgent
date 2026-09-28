"""Deterministic presentation model for a sales-first Company Detail view.

No model calls are made here. The functions only shorten and reorganize V3
data already stored in SQLite, keeping the original memo separately available.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_MARKDOWN_LINK = re.compile(r'\s*\(\s*\[([^\]]+)\]\((https?://[^)]+)\)\s*\)')
_URL = re.compile(r'https?://\S+')


def clean_text(value: object) -> str:
    text = str(value or '').strip()
    text = _MARKDOWN_LINK.sub('', text)
    return _URL.sub('', text).replace('  ', ' ').strip()


def sentences(value: object, limit: int = 3) -> list[str]:
    text = clean_text(value)
    result = [part.strip() + ('。' if not part.strip().endswith(('。', '！', '？')) else '')
              for part in re.split(r'(?<=。)|(?<=！)|(?<=？)', text) if part.strip()]
    return result[:limit]


def _headline(profile: object, signal: object) -> str | None:
    candidates = sentences(profile, 5) + sentences(signal, 3)
    scored = [(sum(word in item for word in ('選定', '認定', '採択', '発表', '開設', 'ローンチ', '出展', '受賞', '締結')), item)
              for item in candidates]
    scored.sort(reverse=True, key=lambda pair: pair[0])
    return scored[0][1] if scored and scored[0][0] else (candidates[0] if candidates else None)


def _opportunity_label(proposal: str, payload: dict[str, Any]) -> str:
    text = proposal + ' ' + str(payload.get('industry') or '')
    if '採用' in text: return '採用強化を伝えるブランド映像'
    if any(word in text for word in ('展示会', '製品デモ', '新製品', '実機')): return '新製品の営業・展示会用映像'
    if any(word in text for word in ('サービス', 'SaaS', '機能')): return '複雑なサービスを伝える紹介映像'
    if any(word in text for word in ('拠点', '開設', '事業拡大')): return '新拠点・事業拡大を伝える映像'
    return '営業で使う説明・紹介映像'


def _proposal(value: object) -> tuple[str, list[str]]:
    text = clean_text(value)
    if not text: return '提案内容は未記録', []
    if '製品デモ' in text: title = '60〜90秒の製品デモ映像' if ('60〜90' in text or '60-90' in text) else '製品デモ映像'
    elif '採用' in text: title = '採用ブランド映像'
    elif 'サービス' in text: title = 'サービス紹介映像'
    else: title = sentences(text, 1)[0].rstrip('。')[:56]
    parts = [part.strip(' ・') for part in re.split(r'[、。]', text) if part.strip()]
    keywords = ('撮影', 'ロケーション', 'モーション', '展示会', '営業', 'Web', '短尺', '実機', '60〜90')
    bullets = [part for part in parts if any(word in part for word in keywords)] or sentences(text, 4)
    return title, bullets[:5]


def _risks(value: object) -> list[str]:
    result: list[str] = []
    for item in sentences(value, 8):
        for part in re.split(r'(?<=必要)で、|(?<=不明)で、', item):
            part = part.rstrip('。').strip()
            if part and part not in result: result.append(part)
    return result[:4]


def _sources(memos: list[dict[str, Any]]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []; seen: set[str] = set()
    for row in memos:
        memo = row.get('memo') or {}
        for evidence in memo.get('evidence') or []:
            if not isinstance(evidence, dict): continue
            url = str(evidence.get('source_url') or '').strip(); title = clean_text(evidence.get('claim') or '')
            if url and url not in seen:
                entries.append({'url': url, 'title': title or urlparse(url).netloc}); seen.add(url)
        for url in row.get('evidence_urls') or []:
            url = str(url).strip()
            if url and url not in seen:
                entries.append({'url': url, 'title': urlparse(url).netloc or 'Source'}); seen.add(url)
    return entries


def build(company: dict[str, Any]) -> dict[str, Any]:
    payload = company.get('payload') or {}
    memo_row = (company.get('memos') or [None])[0]; memo = memo_row.get('memo') if memo_row else {}
    final_row = (company.get('finals') or [None])[0]; final = final_row.get('selection') if final_row else {}
    profile = payload.get('known_company_profile') or ''; signal = (payload.get('recent_signals') or [''])[0]
    proposal_source = final.get('proposed_angle') or memo.get('opportunity_hypothesis') or ''
    proposal_title, proposal_bullets = _proposal(proposal_source)
    return {
        'headline': _headline(profile, signal), 'event_summary': sentences(profile or signal, 3),
        'why_now': sentences(final.get('why_now') or memo.get('why_now'), 3),
        'opportunity': _opportunity_label(proposal_source, payload),
        'why_video': sentences(final.get('why_this_company') or memo.get('why_this_company'), 3),
        'proposal_title': proposal_title, 'proposal_bullets': proposal_bullets,
        'why_us': sentences(final.get('why_this_company') or memo.get('why_this_company'), 2),
        'risks': _risks(final.get('main_risk') or (memo.get('reasons_not_to_pursue') or [''])[0]),
        'sources': _sources(company.get('memos') or []),
        # Detail accordions retain research but never render embedded Markdown URLs.
        'details': {key: ([clean_text(item) for item in value] if isinstance(value, list) else clean_text(value))
                    for key, value in memo.items()},
        'memo': {key: ([clean_text(item) for item in value] if isinstance(value, list) else clean_text(value))
                 for key, value in memo.items()},
        'final': {key: clean_text(value) for key, value in final.items()},
        'research_run': memo_row.get('run_id') if memo_row else None,
        'final_run': final_row.get('run_id') if final_row else None,
    }
