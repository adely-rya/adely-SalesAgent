"""Cheap, source-specific filtering before Fixed Discovery.

Rules are deliberately conservative: obvious noise is dropped, strong trigger
language passes, and ambiguous events are held for optional AI review.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata
from typing import Literal, Protocol

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import SourceEvent, SourceEventPrefilter, utcnow

log = logging.getLogger(__name__)
PrefilterStatus = Literal['PASS', 'HOLD', 'DROP']


@dataclass(frozen=True)
class SignalGroup:
    name: str
    keywords: tuple[str, ...]
    weight: float
    strong: bool = False
    patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrefilterDecision:
    status: PrefilterStatus
    reason: str
    score: float
    rule: str
    classified_event_type: str


@dataclass(frozen=True)
class EvaluatedEvent:
    event_id: int
    source_type: str
    source_name: str
    title: str
    decision: PrefilterDecision


class SourcePrefilter(Protocol):
    def evaluate(self, event: SourceEvent) -> PrefilterDecision: ...


ATPRESS_POSITIVE_SIGNALS = (
    SignalGroup('rebranding', ('リブランディング', 'ブランド刷新', 'ブランドリニューアル',
        'ci刷新', 'ciを刷新', 'vi刷新', 'viを刷新', 'ロゴ刷新', 'ロゴを刷新',
        'purpose', 'パーパス', 'mvv', 'mission', 'vision', 'value'), 4, True),
    SignalGroup('site_refresh', ('コーポレートサイト刷新', 'コーポレートサイトリニューアル',
        'コーポレートサイトを刷新', 'コーポレートサイトを全面刷新',
        'webサイト刷新', 'webサイトリニューアル', 'webサイトを刷新', 'webサイトをリニューアル',
        '採用サイト刷新', '採用サイトリニューアル', '採用サイトを刷新',
        'ブランドサイト公開'), 4, True),
    SignalGroup('new_business', ('新規事業', '新サービス', '新ブランド', '新会社', '子会社設立',
        '事業転換', '事業再編'), 4, True),
    SignalGroup('market_expansion', ('海外展開', '全国展開', '新市場進出'), 4, True),
    SignalGroup('funding', ('資金調達', 'シリーズa', 'シリーズb', '増資'), 2),
    SignalGroup('management', ('経営体制変更', '代表変更', '代表交代', '社長交代', '事業承継'), 2),
    SignalGroup('hiring', ('採用拡大', '大量採用', '組織拡大'), 2),
    SignalGroup('anniversary', ('周年',), 2,
        patterns=(r'(?:創業|設立)\s*\d{1,3}\s*周年',)),
    SignalGroup('new_facility', ('新拠点', '新施設', '旗艦店', '新ホテル', '新工場', 'ショールーム'), 3, True),
)

ATPRESS_NEGATIVE_SIGNALS = (
    SignalGroup('simple_product', ('商品発売', '新商品発売', '販売開始', '発売開始', 'グッズ発売',
        'キャラクター商品', '限定商品', '新色', '新味', 'コラボ商品', '新作登場',
        'ちびぐるみ', 'アイドルマスター'), 2),
    SignalGroup('promotion', ('セール', 'キャンペーン', 'プレゼント', 'クーポン'), 1),
    SignalGroup('exhibition_only', ('イベント出展', '展示会出展', 'フェス出展', '出展決定'), 2),
    SignalGroup('one_off_event', ('イベント開催', '単発イベント', 'イラスト展', 'ポップアップ',
        '記念イベント'), 2),
    SignalGroup('case_study', ('施工事例', '導入事例'), 2),
    SignalGroup('entertainment_merch', ('キャラクター', 'ゲームグッズ', 'アニメグッズ', 'アイドルグッズ'), 1),
)

VC_EVENT_RULES = (
    ('warning', ('不審', '注意喚起', 'ご注意', 'なりすまし', '詐欺')),
    ('ipo', ('新規上場', '上場承認', '株式上場', '東証グロース', 'ipo')),
    ('m_and_a', ('m&a', '買収', '子会社化', '経営統合', 'グループ入り', 'グループ会社化')),
    ('funding', ('資金調達', 'シリーズa', 'シリーズb', 'シリーズc', '増資')),
    ('investment', ('出資', '投資を実行', '追加投資')),
    ('management', ('経営体制', '代表就任', '代表交代', '社長就任', '社長交代')),
    ('event', ('イベント', 'セミナー', '登壇', 'incubate camp', 'camp開催')),
    ('portfolio_update', ('事業開始', 'サービス開始', '事業拡大', '採用強化')),
)
VC_PASS_TYPES = frozenset({'investment', 'funding', 'ipo', 'm_and_a', 'management'})


def normalize_text(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value).casefold())


def _matches(text: str, group: SignalGroup) -> bool:
    return any(normalize_text(keyword) in text for keyword in group.keywords) or any(
        re.search(pattern, text, re.IGNORECASE) for pattern in group.patterns)


class AtPressPrefilter:
    rule = 'atpress-v1'

    def __init__(self, pass_score: float = 2.0, drop_score: float = -1.0) -> None:
        self.pass_score, self.drop_score = pass_score, drop_score

    def evaluate(self, event: SourceEvent) -> PrefilterDecision:
        text = normalize_text(f'{event.title} {event.summary}')
        positive = [group for group in ATPRESS_POSITIVE_SIGNALS if _matches(text, group)]
        negative = [group for group in ATPRESS_NEGATIVE_SIGNALS if _matches(text, group)]
        positive_score = sum(group.weight for group in positive)
        negative_score = sum(group.weight for group in negative)
        score = positive_score - negative_score
        positive_names = ','.join(group.name for group in positive) or 'none'
        negative_names = ','.join(group.name for group in negative) or 'none'
        detail = f'positive={positive_names}; negative={negative_names}'
        if any(group.strong for group in positive):
            return PrefilterDecision('PASS', f'strong positive; {detail}', score,
                                     self.rule, event.event_type)
        if positive_score >= self.pass_score:
            return PrefilterDecision('PASS', f'positive threshold met; {detail}', score,
                                     self.rule, event.event_type)
        if negative and score <= self.drop_score:
            return PrefilterDecision('DROP', f'noise signals without sufficient positive; {detail}', score,
                                     self.rule, event.event_type)
        return PrefilterDecision('HOLD', f'ambiguous; {detail}', score, self.rule, event.event_type)


class VCNewsPrefilter:
    rule = 'vc-news-v1'

    def classify(self, event: SourceEvent) -> str:
        text = normalize_text(f'{event.title} {event.summary}')
        for event_type, keywords in VC_EVENT_RULES:
            if any(normalize_text(keyword) in text for keyword in keywords):
                return event_type
        return event.event_type if event.event_type in VC_PASS_TYPES else 'other'

    def evaluate(self, event: SourceEvent) -> PrefilterDecision:
        event_type = self.classify(event)
        if event_type == 'warning':
            return PrefilterDecision('DROP', 'VC warning or fraud notice', -2, self.rule, event_type)
        if event_type in VC_PASS_TYPES:
            return PrefilterDecision('PASS', f'high-value VC event type: {event_type}', 3,
                                     self.rule, event_type)
        return PrefilterDecision('HOLD', f'VC event requires content review: {event_type}', 0,
                                 self.rule, event_type)


class DefaultPrefilter:
    rule = 'default-safe-v1'

    def evaluate(self, event: SourceEvent) -> PrefilterDecision:
        return PrefilterDecision('HOLD', 'Unknown source; retained for safe review', 0,
                                 self.rule, event.event_type or 'other')


def source_prefilters(settings: Settings) -> dict[str, SourcePrefilter]:
    return {
        'atpress': AtPressPrefilter(settings.atpress_prefilter_pass_score,
                                    settings.atpress_prefilter_drop_score),
        'vc_news': VCNewsPrefilter(),
    }


def evaluate_source_event(event: SourceEvent, settings: Settings) -> PrefilterDecision:
    return source_prefilters(settings).get(event.source_type, DefaultPrefilter()).evaluate(event)


def events_without_prefilter(session: Session, limit: int) -> list[SourceEvent]:
    result_exists = select(SourceEventPrefilter.id).where(
        SourceEventPrefilter.source_event_id == SourceEvent.id).exists()
    return list(session.scalars(select(SourceEvent).where(
        SourceEvent.processed_at.is_(None), ~result_exists,
    ).order_by(SourceEvent.published_at.desc(), SourceEvent.id.desc()).limit(limit)))


def prefilter_source_events(session: Session, events: list[SourceEvent], settings: Settings) -> list[EvaluatedEvent]:
    evaluated: list[EvaluatedEvent] = []
    for event in events:
        decision = evaluate_source_event(event, settings)
        stored = session.scalar(select(SourceEventPrefilter).where(
            SourceEventPrefilter.source_event_id == event.id))
        if stored is None:
            stored = SourceEventPrefilter(source_event_id=event.id)
            session.add(stored)
        stored.status, stored.reason, stored.score = decision.status, decision.reason, decision.score
        stored.rule, stored.classified_event_type = decision.rule, decision.classified_event_type
        stored.prefiltered_at = utcnow()
        event.event_type = decision.classified_event_type
        if decision.status == 'DROP':
            event.processed_at = utcnow()
        evaluated.append(EvaluatedEvent(event.id, event.source_type, event.source_name, event.title, decision))
    session.flush()
    return evaluated


def eligible_source_events(session: Session, limit: int, include_hold: bool) -> list[SourceEvent]:
    statuses = ['PASS', 'HOLD'] if include_hold else ['PASS']
    priority = case((SourceEventPrefilter.status == 'PASS', 0), else_=1)
    return list(session.scalars(select(SourceEvent).join(SourceEventPrefilter,
        SourceEventPrefilter.source_event_id == SourceEvent.id).where(
            SourceEvent.processed_at.is_(None), SourceEventPrefilter.status.in_(statuses),
        ).order_by(priority, SourceEvent.published_at.desc(), SourceEvent.id.desc()).limit(limit)))


def summarize_prefilter(events: list[EvaluatedEvent]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for event in events:
        counts = summary.setdefault(event.source_name, {'collected': 0, 'pass': 0, 'hold': 0, 'drop': 0})
        counts['collected'] += 1
        counts[event.decision.status.lower()] += 1
    return summary


def log_prefilter_metrics(events: list[EvaluatedEvent]) -> None:
    for source_name, counts in summarize_prefilter(events).items():
        log.info('source prefilter source=%s collected=%d pass=%d hold=%d drop=%d',
                 source_name, counts['collected'], counts['pass'], counts['hold'], counts['drop'])
