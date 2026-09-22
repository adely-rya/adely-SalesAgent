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
    '地域 × 製造業・メーカー × 新商品・設備新設・ブランド刷新（神奈川・東京を優先）',
    '地域 × B2B・建築・工務店・不動産 × 新サービス・拠点・ショールーム・顧客層の変化',
    '地域 × 採用・企業発信 × 採用強化・採用サイト刷新・新卒/中途採用・採用広報',
    '地域 × 食品・消費財・地域サービス × 商品/パッケージ変更・店舗/地域展開・周年',
    'Technology / SaaS / DeepTech × 複雑な新サービス・新事業・新しい説明対象（startup以外も含む）',
    '企業の小さなCommunication Trigger × Web/SNS活性化・展示会・OEM・ブランドメッセージ変更',
]
log = logging.getLogger(__name__)


async def discover_topic(client: LLMClient, settings: Settings, topic: str) -> tuple[list[Candidate], set[str], int]:
    now = datetime.now(ZoneInfo(settings.timezone))
    log.info('discovery topic started topic=%s', topic)
    result = await client.generate(model=settings.discovery_model, instructions=client.prompt('legacy_discovery'),
        input_text=json.dumps({'topic': topic, 'today': str(now.date()),
            'preferred_since': str((now - timedelta(days=30)).date()),
            'candidate_schema': Candidate.model_json_schema()}, ensure_ascii=False),
        output_type=DiscoveryOutput, use_web_search=True,
        reasoning_effort=settings.discovery_reasoning_effort)
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
