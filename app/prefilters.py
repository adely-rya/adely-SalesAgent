"""Deterministic source-specific filtering and routing before Fixed Discovery."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
import unicodedata
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import SourceEvent, SourceEventPrefilter, utcnow

log = logging.getLogger(__name__)
PrefilterStatus = Literal['PASS', 'HOLD', 'DROP']


@dataclass(frozen=True)
class SignalGroup:
    name: str
    patterns: tuple[str, ...]
    strength: float
    event_type: str = 'other'
    strong: bool = False


@dataclass(frozen=True)
class PrefilterDecision:
    status: PrefilterStatus
    reason: str
    score: float
    rule: str
    classified_event_type: str
    event_strength: float
    matched_positive_signals: tuple[str, ...] = ()
    matched_negative_signals: tuple[str, ...] = ()
    supporting_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluatedEvent:
    event_id: int
    source_type: str
    source_name: str
    title: str
    decision: PrefilterDecision


@dataclass(frozen=True)
class RoutedEvent:
    event: SourceEvent
    prefilter: SourceEventPrefilter
    priority: float


class SourcePrefilter(Protocol):
    def evaluate(self, event: SourceEvent) -> PrefilterDecision: ...


# Small, tuneable local routing constants. Event strength remains dominant.
SOURCE_RELIABILITY_STRENGTH_BONUS = {'vc_news': 5.0, 'atpress': 0.0}
SOURCE_ROUTING_BONUS = {'vc_news': 8.0, 'atpress': 0.0}
SOURCE_DIVERSITY_PENALTY = 3.0
RECENCY_MAX_BONUS = 5.0
RECENCY_WINDOW_DAYS = 30
UNKNOWN_SOURCE_STRENGTH = 50.0

ATPRESS_POSITIVE_SIGNALS = (
    SignalGroup('rebranding', (r'(?:リブランディング|リ・ブランディング|rebranding)', r'ブランド(?:を)?(?:刷新|リニューアル)',
        r'ブランド.{0,50}(?:を)?(?:全面)?(?:刷新|リニューアル)',
        r'(?:ci|vi|ロゴ)(?:を)?刷新', r'(?:purpose|パーパス|mvv|mission|vision|value)(?:を)?(?:策定|変更|刷新)'),
        88, 'rebranding', True),
    SignalGroup('site_refresh', (r'(?:コーポレート|公式|採用|web)?サイト(?:を)?(?:全面)?(?:刷新|リニューアル)',
        r'ホームページ(?:を)?(?:全面)?(?:刷新|リニューアル)'), 72, 'site_refresh', True),
    SignalGroup('new_business', (r'新規事業(?:として)?.{0,80}?(?:を)?開始', r'新サービス(?:を)?開始',
        r'新ブランド(?:を)?(?:発表|開始|立ち上げ)', r'(?:新会社|子会社)(?:を)?設立', r'(?:事業転換|事業再編)'),
        82, 'new_business', True),
    SignalGroup('market_expansion', (r'(?:海外|全国)展開', r'新市場(?:に)?進出'), 78, 'market_expansion', True),
    SignalGroup('funding', (r'資金調達', r'シリーズ[abc]', r'増資'), 75, 'funding'),
    SignalGroup('management', (r'経営体制(?:を)?変更', r'(?:代表|社長)(?:を)?(?:変更|交代)', r'事業承継'), 68, 'management'),
    SignalGroup('hiring', (r'(?:採用|組織)(?:を)?拡大', r'大量採用'), 60, 'hiring'),
    SignalGroup('new_facility', (r'(?:新拠点|新施設|旗艦店|新ホテル|新工場|ショールーム)(?:を)?(?:開設|オープン|設立)',),
        62, 'facility', True),
)
# Anniversary is supporting evidence only.  A business transformation must carry it.
ATPRESS_SUPPORTING_SIGNALS = (SignalGroup('anniversary', (r'(?:創業|設立)?\d{1,3}周年',), 40, 'anniversary'),)
ATPRESS_NEGATIVE_SIGNALS = (
    SignalGroup('simple_product', (r'(?:新)?商品(?:を)?発売', r'販売(?:を)?開始', r'発売(?:を)?開始',
        r'グッズ(?:を)?発売', r'限定商品', r'新[色味]', r'コラボ商品', r'新作(?:を)?(?:発売|登場)',
        r'ちびぐるみ', r'アイドルマスター', r'ぷにっこ'), 12, 'product'),
    SignalGroup('promotion', (r'セール', r'キャンペーン', r'プレゼント', r'クーポン'), 15, 'promotion'),
    SignalGroup('exhibition_only', (r'(?:イベント|展示会|フェス)(?:に|へ)?出展', r'出展決定', r'イラスト展', r'鳥フェス'), 15, 'event'),
    SignalGroup('one_off_event', (r'イベント(?:を)?開催', r'単発イベント', r'ポップアップ', r'記念イベント'), 20, 'event'),
    SignalGroup('case_study', (r'(?:施工|導入)事例',), 10, 'case_study'),
    SignalGroup('entertainment_anniversary', (r'(?:記念)?(?:アルバム|ゲーム|アニメ|キャラクター)(?:を)?(?:発売|公開|周年)',
        r'(?:音楽活動|バンド)\d{1,3}周年'), 15, 'entertainment'),
    SignalGroup('entertainment_merch', (r'キャラクター', r'ゲームグッズ', r'アニメグッズ', r'アイドルグッズ'), 10, 'entertainment'),
)

VC_CATEGORY_TYPES = {
    '注意喚起': 'warning', 'warning': 'warning',
    'メディア掲載': 'media', 'メディア': 'media', '掲載': 'media', 'press': 'media',
    '投資': 'investment', '出資': 'investment', '新規投資': 'investment',
    '追加投資': 'investment', '新規・追加投資': 'investment', 'investment': 'investment',
    '資金調達': 'funding', 'funding': 'funding',
    'ipo': 'ipo', '上場': 'ipo',
    'm&a': 'm_and_a', '買収': 'm_and_a', '経営統合': 'm_and_a',
    '人事': 'management', '経営': 'management', '代表': 'management',
    'イベント': 'event', 'セミナー': 'event',
}
VC_PASS_TYPES = frozenset({'investment', 'funding', 'ipo', 'm_and_a', 'management'})
VC_STRENGTHS = {'investment': 95, 'funding': 92, 'ipo': 98, 'm_and_a': 94, 'management': 72,
                'portfolio_update': 65, 'media': 10, 'warning': 0, 'event': 25, 'other': 50}
VC_INVESTMENT_EXCLUSIONS = (r'投資家(?:ランキング|インタビュー|向け|情報)?', r'ベンチャー投資家',
                            r'投資(?:家)?向け(?:イベント|セミナー|情報)')


def normalize_text(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value).casefold())


def _matches(text: str, group: SignalGroup) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in group.patterns)


def _event_categories(event: SourceEvent) -> tuple[str, ...]:
    raw_data = event.raw_data if isinstance(event.raw_data, dict) else {}
    labels = []
    for key in ('category', 'categories', 'category_name', 'source_category'):
        value = raw_data.get(key)
        if isinstance(value, (list, tuple, set)):
            labels.extend(str(item) for item in value if item)
        elif value:
            labels.append(str(value))
    return tuple(normalize_text(label) for label in labels)


def _category_type(category: str) -> str | None:
    return VC_CATEGORY_TYPES.get(category)


def _bounded_strength(value: float) -> float:
    return max(0.0, min(100.0, value))


class AtPressPrefilter:
    rule = 'atpress-v2'

    def __init__(self, pass_score: float = 2.0, drop_score: float = -1.0) -> None:
        self.pass_score, self.drop_score = pass_score, drop_score

    def evaluate(self, event: SourceEvent) -> PrefilterDecision:
        text = normalize_text(f'{event.title} {event.summary}')
        positive = [group for group in ATPRESS_POSITIVE_SIGNALS if _matches(text, group)]
        negative = [group for group in ATPRESS_NEGATIVE_SIGNALS if _matches(text, group)]
        supporting = [group for group in ATPRESS_SUPPORTING_SIGNALS if _matches(text, group)]
        positive_names, negative_names = tuple(item.name for item in positive), tuple(item.name for item in negative)
        supporting_names = tuple(item.name for item in supporting)
        detail = f'positive={",".join(positive_names) or "none"}; negative={",".join(negative_names) or "none"}; supporting={",".join(supporting_names) or "none"}'
        score = len(positive) * 2 - len(negative) * 2
        event_type = (max(positive, key=lambda item: item.strength).event_type if positive else
                      min(negative, key=lambda item: item.strength).event_type if negative else
                      'anniversary' if supporting else event.event_type or 'other')
        if any(item.strong for item in positive):
            strength = max(item.strength for item in positive) + (2 if supporting else 0)
            return PrefilterDecision('PASS', f'strong_business_trigger; {detail}', score, self.rule, event_type,
                                     _bounded_strength(strength), positive_names, negative_names, supporting_names)
        if positive:
            return PrefilterDecision('PASS', f'business_trigger; {detail}', score, self.rule, event_type,
                                     max(item.strength for item in positive), positive_names, negative_names, supporting_names)
        if negative and any(item.name != 'one_off_event' for item in negative):
            return PrefilterDecision('DROP', f'noise_signal; {detail}', score, self.rule, event_type,
                                     min(item.strength for item in negative), positive_names, negative_names, supporting_names)
        if supporting:
            return PrefilterDecision('HOLD', f'anniversary_supporting_signal_only; {detail}', score, self.rule,
                                     event_type, 40, positive_names, negative_names, supporting_names)
        if negative:
            return PrefilterDecision('HOLD', f'low_strength_event; {detail}', score, self.rule, event_type,
                                     min(item.strength for item in negative), positive_names, negative_names, supporting_names)
        return PrefilterDecision('HOLD', f'ambiguous; {detail}', score, self.rule, event_type,
                                 UNKNOWN_SOURCE_STRENGTH, positive_names, negative_names, supporting_names)


class VCNewsPrefilter:
    rule = 'vc-news-v2'

    def classify(self, event: SourceEvent) -> tuple[str, str]:
        text = normalize_text(f'{event.title} {event.summary}')
        if any(re.search(pattern, text) for pattern in (r'不審', r'注意喚起', r'ご注意', r'なりすまし', r'詐欺', r'不正使用.{0,20}注意')):
            return 'warning', 'title_warning_pattern'
        category_types = [_category_type(category) for category in _event_categories(event)]
        # Negative source labels win over a conflicting positive label so a
        # mis-tagged warning/media article cannot route as a business trigger.
        if 'warning' in category_types:
            return 'warning', 'source_category'
        if 'media' in category_types:
            return 'media', 'source_category'
        category_type = next((item for item in category_types if item), None)
        if category_type:
            return category_type, 'source_category'
        if (re.search(r'(?:東京証券取引所|東証).{0,40}(?:市場)?(?:へ|に)?上場', text) or
                re.search(r'(?:新規上場|上場承認|株式上場|\bipo\b)', text, re.IGNORECASE)):
            return 'ipo', 'listed_on_exchange'
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in (r'm&a', r'買収', r'子会社化', r'経営統合', r'グループ入り', r'グループ会社化')):
            return 'm_and_a', 'title_ma_pattern'
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in (r'資金調達', r'シリーズ[abc]', r'増資')):
            return 'funding', 'title_funding_pattern'
        if not any(re.search(pattern, text) for pattern in VC_INVESTMENT_EXCLUSIONS):
            if any(re.search(pattern, text) for pattern in (r'(?:に|へ)出資(?:しました|を実行)?', r'投資を実行', r'追加投資')):
                return 'investment', 'title_investment_pattern'
        if re.search(r'(?:経営体制|代表|社長).{0,12}(?:就任|交代|変更)', text):
            return 'management', 'title_management_pattern'
        if any(re.search(pattern, text) for pattern in (r'ランキング', r'forbes', r'メディア掲載', r'インタビュー',
                                                        r'(?:日経|forbes).{0,30}(?:掲載|記事|ランキング)', r'(?:取材|対談)記事')):
            return 'media', 'title_media_pattern'
        if any(re.search(pattern, text) for pattern in (r'イベント', r'セミナー', r'登壇', r'incubatecamp', r'camp開催')):
            return 'event', 'title_event_pattern'
        if any(re.search(pattern, text) for pattern in (r'事業(?:を)?開始', r'サービス(?:を)?開始', r'事業拡大', r'採用強化')):
            return 'portfolio_update', 'title_portfolio_pattern'
        return (event.event_type if event.event_type in VC_STRENGTHS else 'other'), 'safe_default'

    def evaluate(self, event: SourceEvent) -> PrefilterDecision:
        event_type, match_rule = self.classify(event)
        strength = _bounded_strength(VC_STRENGTHS[event_type] + SOURCE_RELIABILITY_STRENGTH_BONUS['vc_news'])
        if event_type == 'warning':
            return PrefilterDecision('DROP', f'{match_rule}: warning', -2, self.rule, event_type, 0, (), ('warning',), ())
        if event_type == 'media':
            return PrefilterDecision('DROP', f'{match_rule}: media', -1, self.rule, event_type, strength, (), ('media',), ())
        if event_type in VC_PASS_TYPES:
            return PrefilterDecision('PASS', f'{match_rule}: {event_type}', 3, self.rule, event_type, strength, (event_type,), (), ())
        return PrefilterDecision('HOLD', f'{match_rule}: {event_type}', 0, self.rule, event_type, strength, (), (), (event_type,))


class DefaultPrefilter:
    rule = 'default-safe-v2'
    def evaluate(self, event: SourceEvent) -> PrefilterDecision:
        return PrefilterDecision('HOLD', 'Unknown source; retained for safe review', 0, self.rule,
                                 event.event_type or 'other', UNKNOWN_SOURCE_STRENGTH)


def source_prefilters(settings: Settings) -> dict[str, SourcePrefilter]:
    return {'atpress': AtPressPrefilter(settings.atpress_prefilter_pass_score, settings.atpress_prefilter_drop_score),
            'vc_news': VCNewsPrefilter()}


def evaluate_source_event(event: SourceEvent, settings: Settings) -> PrefilterDecision:
    return source_prefilters(settings).get(event.source_type, DefaultPrefilter()).evaluate(event)


def extract_raw_item_decisions(raw_items: list[object], settings: Settings) -> dict[int, PrefilterDecision]:
    """Classify all loaded raw items in memory; persistence happens after the run."""
    return {item.id: evaluate_source_event(item, settings) for item in raw_items}


def route_raw_items(raw_items: list[object], decisions: dict[int, PrefilterDecision], limit: int,
                    include_hold: bool) -> list[object]:
    """Select eligible Raw Items using strength first and source diversity second."""
    statuses = {'PASS', 'HOLD'} if include_hold else {'PASS'}
    remaining = [item for item in raw_items if decisions[item.id].status in statuses]
    selected: list[object] = []
    selected_per_source: defaultdict[str, int] = defaultdict(int)
    while remaining and len(selected) < limit:
        def priority(item: object) -> float:
            published_at = item.published_at
            return (decisions[item.id].event_strength + SOURCE_ROUTING_BONUS.get(item.source_type, 0.0)
                    + _recency_bonus(published_at) - SOURCE_DIVERSITY_PENALTY * selected_per_source[item.source_name])
        selected_item = max(remaining, key=lambda item: (priority(item), item.id))
        selected.append(selected_item)
        selected_per_source[selected_item.source_name] += 1
        remaining.remove(selected_item)
    return selected


def events_without_prefilter(session: Session, limit: int) -> list[SourceEvent]:
    result_exists = select(SourceEventPrefilter.id).where(SourceEventPrefilter.source_event_id == SourceEvent.id).exists()
    return list(session.scalars(select(SourceEvent).where(SourceEvent.processed_at.is_(None), ~result_exists).
                                order_by(SourceEvent.published_at.desc(), SourceEvent.id.desc()).limit(limit)))


def prefilter_source_events(session: Session, events: list[SourceEvent], settings: Settings) -> list[EvaluatedEvent]:
    evaluated: list[EvaluatedEvent] = []
    event_ids = [event.id for event in events]
    existing_rows = {row.source_event_id: row for row in session.scalars(select(SourceEventPrefilter).where(
        SourceEventPrefilter.source_event_id.in_(event_ids))).all()} if event_ids else {}
    now = utcnow()
    for event in events:
        decision = evaluate_source_event(event, settings)
        stored = existing_rows.get(event.id)
        if stored is None:
            stored = SourceEventPrefilter(source_event_id=event.id)
            session.add(stored)
            existing_rows[event.id] = stored
        stored.status, stored.reason, stored.score = decision.status, decision.reason, decision.score
        stored.rule, stored.classified_event_type = decision.rule, decision.classified_event_type
        stored.event_strength = decision.event_strength
        stored.matched_positive_signals = list(decision.matched_positive_signals)
        stored.matched_negative_signals = list(decision.matched_negative_signals)
        stored.supporting_signals = list(decision.supporting_signals)
        stored.prefiltered_at = now
        event.event_type = decision.classified_event_type
        if decision.status == 'DROP':
            event.processed_at = now
        evaluated.append(EvaluatedEvent(event.id, event.source_type, event.source_name, event.title, decision))
    session.flush()
    return evaluated


def _recency_bonus(published_at: datetime | None, now: datetime | None = None) -> float:
    if published_at is None:
        return 0.0
    now = now or datetime.now(timezone.utc)
    value = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - value).total_seconds() / 86400)
    return max(0.0, RECENCY_MAX_BONUS * (1 - min(age_days, RECENCY_WINDOW_DAYS) / RECENCY_WINDOW_DAYS))


def route_candidate_events(records: list[tuple[SourceEvent, SourceEventPrefilter]], limit: int) -> list[RoutedEvent]:
    """Strength-first routing with small VC/recency bonuses and soft diversity."""
    remaining, selected = list(records), []
    selected_by_source: defaultdict[str, int] = defaultdict(int)
    while remaining and len(selected) < limit:
        def priority(item: tuple[SourceEvent, SourceEventPrefilter]) -> float:
            event, prefilter = item
            return (prefilter.event_strength + SOURCE_ROUTING_BONUS.get(event.source_type, 0.0) + _recency_bonus(event.published_at)
                    - SOURCE_DIVERSITY_PENALTY * selected_by_source[event.source_name])
        event, prefilter = max(remaining, key=lambda item: (priority(item), item[0].id))
        selected.append(RoutedEvent(event, prefilter, priority((event, prefilter))))
        selected_by_source[event.source_name] += 1
        remaining.remove((event, prefilter))
    return selected


def eligible_source_events(session: Session, limit: int, include_hold: bool, settings: Settings | None = None) -> list[SourceEvent]:
    del settings
    statuses = ['PASS', 'HOLD'] if include_hold else ['PASS']
    records = list(session.execute(select(SourceEvent, SourceEventPrefilter).join(
        SourceEventPrefilter, SourceEventPrefilter.source_event_id == SourceEvent.id).where(
            SourceEvent.processed_at.is_(None), SourceEventPrefilter.status.in_(statuses))))
    return [route.event for route in route_candidate_events(records, limit)]


def summarize_prefilter(events: list[EvaluatedEvent]) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    strengths: defaultdict[str, list[float]] = defaultdict(list)
    for event in events:
        counts = summary.setdefault(event.source_name, {'collected': 0, 'pass': 0, 'hold': 0, 'drop': 0, 'avg_strength': 0.0})
        counts['collected'] += 1
        counts[event.decision.status.lower()] += 1
        strengths[event.source_name].append(event.decision.event_strength)
    for source_name, values in strengths.items():
        summary[source_name]['avg_strength'] = round(sum(values) / len(values), 1)
    return summary


def routing_metrics(session: Session, events: list[SourceEvent]) -> dict[str, object]:
    if not events:
        return {'selected': 0, 'selected_avg_strength': 0.0, 'sources': {}}
    rows = list(session.scalars(select(SourceEventPrefilter).where(SourceEventPrefilter.source_event_id.in_([event.id for event in events]))))
    strengths = {row.source_event_id: row.event_strength for row in rows}
    sources: defaultdict[str, int] = defaultdict(int)
    for event in events:
        sources[event.source_name] += 1
    return {'selected': len(events), 'selected_avg_strength': round(sum(strengths.get(event.id, 0) for event in events) / len(events), 1),
            'sources': dict(sorted(sources.items()))}


def log_prefilter_metrics(events: list[EvaluatedEvent]) -> None:
    for source_name, counts in summarize_prefilter(events).items():
        log.info('source prefilter source=%s collected=%d pass=%d hold=%d drop=%d avg_strength=%.1f', source_name,
                 counts['collected'], counts['pass'], counts['hold'], counts['drop'], counts['avg_strength'])
