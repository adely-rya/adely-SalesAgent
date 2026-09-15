from datetime import datetime, timedelta
import json
import logging
from zoneinfo import ZoneInfo
from pydantic import ValidationError
from app.schemas import Candidate, DiscoveryOutput
from app.config import Settings
from app.llm import LLMClient
from app.deduplication import canonical_url

DISCOVERY_TOPICS = [
    '新ブランド 新サービス ローンチ',
    '新規事業 新市場進出',
    '資金調達 スタートアップ 事業拡大',
    'リブランディング 理念刷新',
    '新店舗 施設 ホテル 開業',
    '周年 組織変革',
]
log = logging.getLogger(__name__)


async def discover_topic(client: LLMClient, settings: Settings, topic: str) -> tuple[list[Candidate], set[str], int]:
    now = datetime.now(ZoneInfo(settings.timezone))
    log.info('discovery topic started topic=%s', topic)
    result = await client.generate(model=settings.discovery_model, instructions=client.prompt('discovery'),
        input_text=json.dumps({'topic': topic, 'today': str(now.date()),
            'preferred_since': str((now - timedelta(days=30)).date()),
            'candidate_schema': Candidate.model_json_schema()}, ensure_ascii=False),
        output_type=DiscoveryOutput, use_web_search=True, reasoning_effort='xhigh')
    evidence = {canonical_url(url) for url in result.evidence_urls}
    candidates, rejected = [], 0
    for raw in result.value.candidates:
        try:
            candidate = Candidate.model_validate(raw)
            if canonical_url(str(candidate.source_url)) not in evidence:
                raise ValueError('Unverified source')
            if any(canonical_url(str(fact.source_url)) not in evidence for fact in candidate.research_facts):
                raise ValueError('Unverified research source')
            if candidate.published_at and candidate.published_at > now.date():
                raise ValueError('Future publication date')
            # System clock is authoritative; model timestamps are never trusted.
            candidate.discovered_at = now
            candidates.append(candidate)
        except (ValidationError, ValueError):
            rejected += 1
    log.info('companies discovered topic=%s accepted=%d rejected=%d', topic, len(candidates), rejected)
    return candidates, evidence, rejected
