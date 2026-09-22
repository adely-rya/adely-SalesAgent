"""Deterministic hard filters and the low-cost, no-search WIN pre-evaluation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from app.config import Settings
from app.llm import Generation, LLMClient
from app.domain import Opportunity
from app.schemas import CheapWinOutput


# Growth signals support context, but do not by themselves allocate Research.
GROWTH_SIGNAL_PATTERNS = {
    'capital_or_listing': re.compile(
        r'\b(?:funding|fundraising|investment|ipo|m_and_a|acquisition|merger)\b|'
        r'資金調達|出資|増資|上場|買収|借入', re.I),
    'market_or_footprint_expansion': re.compile(
        r'\b(?:overseas|international|expansion|new_market|new_company|partnership|alliance)\b|'
        r'海外.{0,8}(?:展開|進出|拡大|拡張|参入)|新市場|新会社|業務提携|戦略提携|'
        r'拠点拡大|新拠点|多拠点|施設開業|新店舗|ホテル.{0,8}開業|売上.{0,4}(?:成長|拡大)', re.I),
    'team_or_organization_growth': re.compile(
        r'\b(?:hiring|recruitment|organization|management|succession)\b|'
        r'採用拡大|採用強化|人員拡大|組織拡大|組織変更|経営交代|新経営体制', re.I),
}

# These are specific changes to identity, business model, target, or expression.
# A generic appearance of the word "brand" is deliberately insufficient.
STRONG_COMMUNICATION_PATTERNS = {
    'identity_or_expression_change': re.compile(
        r'\brebrand(?:ing)?\b|\bcorporate[_ -]?(?:identity|site|brand)(?:[_ -]?(?:renewal|refresh))?\b|'
        r'\b(?:recruit|brand)[_ -]?site\b|\bpurpose\b|\bmission\b|\bvision\b|'
        r'\bmvv\b|\bci\b|\bvi\b|\bcompany[_ -]?name[_ -]?change\b|\blogo[_ -]?refresh\b|'
        r'ブランド.{0,6}(?:刷新|再定義|再構築|リニューアル|変更)|企業名変更|社名変更|'
        r'企業理念.{0,6}(?:刷新|改定|変更)|理念体系刷新|MVV|パーパス|スローガン刷新|'
        r'CI刷新|VI刷新|コーポレート[・ _-]?.{0,6}(?:ブランド|ロゴ|サイト|アイデンティティ).{0,8}'
        r'(?:刷新|全面|変更|改定|リニューアル)|(?:採用|ブランド)サイト.{0,6}(?:刷新|リニューアル)|'
        r'公式サイト.{0,6}(?:刷新|リニューアル)|働き方.{0,8}(?:刷新|改革|変更)|'
        r'採用体験.{0,8}(?:刷新|変更)', re.I),
    'business_or_audience_shift': re.compile(
        r'\bmarket[_ -]?entry\b|\bnew[_ -]?target\b|'
        r'\bbusiness[_ -]?(?:model|transformation)\b|'
        r'新市場参入|新ターゲット|事業モデル|事業転換|新領域展開|多角化|'
        r'事業承継|MBO|企業文化変革|組織変革|組織再編|新経営方針', re.I),
    'employer_branding_or_recruit_expression': re.compile(
        r'\bemployer[_ -]?branding\b|\brecruit[_ -]?site[_ -]?(?:refresh|renewal)\b|'
        r'採用ブランディング|採用サイト.{0,6}(?:刷新|リニューアル)', re.I),
}

# A new-service/product label is only a medium signal. A clearly technical or
# multi-system B2B offer can instead be a Communication Trigger on its own.
MEDIUM_COMMUNICATION_PATTERNS = {
    'new_service_or_product': re.compile(
        r'\bnew[_ -]?(?:service|product)(?:[_ -]?(?:launch|start|release))?\b|'
        r'\bservice[_ -]?launch\b|'
        r'新サービス|サービス開始|製品化|商品化|新製品', re.I),
    'new_brand_or_company': re.compile(
        r'\bnew[_ -]?(?:brand|company)(?:[_ -]?(?:launch|start|formation))?\b|'
        r'新ブランド|ブランド立ち上げ|新会社設立|新会社始動', re.I),
    'new_business_or_market_entry': re.compile(
        r'\bnew[_ -]?(?:business|market|target)(?:[_ -]?(?:launch|start|entry))?\b|'
        r'新規事業|新市場参入|新ターゲット|新領域展開|AI事業拡大', re.I),
    'brand_or_customer_experience_expansion': re.compile(
        r'ブランド体験|顧客体験.{0,6}(?:拡大|拡張|展開)', re.I),
}

COMPLEX_SERVICE_CONTEXT = re.compile(
    r'(?<![A-Za-z])(?:AI|B2B|DeepTech|IoT|RFID|SaaS|robotics)(?![A-Za-z])|ロボット|技術|法人向け|'
    r'サプライチェーン|セキュリティ|通信基盤|データベース|自律型|複数.{0,8}(?:事業|サービス)', re.I)
COMMUNICATION_HYPOTHESIS = re.compile(
    r'(?:映像|動画).{0,24}(?:説明|紹介|採用|営業|技術|ブランド|事業|サービス|顧客|社内)|'
    r'(?:説明|紹介|採用|営業|技術|ブランド|事業|サービス|顧客|社内).{0,24}(?:映像|動画|伝える|共有|発信)', re.I)
IPO_EVENT_TYPE = re.compile(
    r'IPO(?:を)?(?:実施|達成|上場)|株式公開|(?:東京証券取引所.{0,24}市場|(?:東証)?(?:プライム|スタンダード|グロース)市場)'
    r'.{0,12}(?:上場|新規上場)|(?:新規)?上場(?:承認|しました|いたしました|へ|を)', re.I)


@dataclass(frozen=True)
class HardFilterResult:
    excluded: bool
    risk_tags: tuple[str, ...]
    reason: str

    def model_dump(self) -> dict:
        return {'excluded': self.excluded, 'risk_tags': list(self.risk_tags), 'reason': self.reason}


@dataclass(frozen=True)
class EventSemantics:
    """Internal, machine-readable interpretation of a human-readable Event.

    This is intentionally not persisted as another Event schema: the original
    type/title/summary remain the evidence, and these labels only help Gate
    compare equivalent descriptions consistently.
    """
    canonical_types: tuple[str, ...]
    growth_signals: tuple[str, ...]
    strong_communication_signals: tuple[str, ...]
    medium_communication_signals: tuple[str, ...]


_PARTNERSHIP = re.compile(
    r'\b(?:partnership|alliance|collaboration)\b|業務提携|戦略提携|協業|共同(?:開発|実証|研究)|'
    r'パートナーシップ|共同キャンペーン', re.I)
_EVENT_OR_SEMINAR = re.compile(
    r'\b(?:seminar|webinar|event|conference)\b|セミナー|ウェビナー|講演会|説明会|イベント.{0,8}(?:開催|出展)|'
    r'(?:開催|実施).{0,8}(?:セミナー|ウェビナー|講演会|イベント)', re.I)
_CAMPAIGN = re.compile(r'キャンペーン|共同プロモーション|販促企画', re.I)
_PRICING_UPDATE = re.compile(r'料金体系|価格改定|価格変更|料金改定|プラン改定|料金プラン変更', re.I)
_FEATURE_UPDATE = re.compile(r'既存.{0,10}(?:機能|サービス).{0,12}(?:追加|改修|改善|AIエージェント化)|機能追加|機能改善', re.I)
_SERVICE_LAUNCH = re.compile(
    r'(?:新サービス|サービス|ソリューション|プロダクト|製品|商品|インフラ|基盤|'
    r'プラットフォーム|ワークスペース|支援サービス)'
    r'[^。！？\n]{0,36}(?:提供開始|提供を開始|正式提供|正式公開|公開を開始|公開|'
    r'リリース|ローンチ|販売開始|販売を開始|サービス開始|サービスを開始|を開始|開始)|'
    r'(?:AI|人工知能)[^。！？\n]{0,24}(?:提供開始|提供を開始|正式提供|正式公開|公開を開始|公開|'
    r'リリース|ローンチ|販売開始)|'
    r'\b(?:new[_ -])?(?:service|solution|product|platform|infrastructure|workspace)\b'
    r'.{0,40}\b(?:launch(?:ed)?|release(?:d)?|now available|offering)\b', re.I)
_SITE_RENEWAL = re.compile(
    r'(?:コーポレート|コーポレートサイト|採用|採用サイト|ブランド|ブランドサイト|公式)'
    r'.{0,10}(?:サイト)?(?:.{0,8})?(?:刷新|全面リニューアル|リニューアル|renewal)', re.I)

_EVENT_TYPE_ALIASES = {
    'fundraising': 'funding', 'investment': 'funding', 'listing': 'ipo',
    'new_business_launch': 'new_business', 'new_service_launch': 'new_service',
    'new_product_launch': 'new_service', 'ci_vi_change': 'rebrand', 'mvv_change': 'rebrand',
    'corporate_site_renewal': 'site_renewal', 'seminar': 'event', 'webinar': 'event',
    'store_renovation': 'facility_opening',
}


def classify_event_semantics(event) -> EventSemantics:
    """Classify Event meaning from its type, title, and summary together.

    Specific action context outranks generic labels: a seminar about a service
    is not a service launch, and a partnership/campaign is not a rebrand. The
    classifier is deliberately small and conservative; it never infers company
    attributes from prose.
    """
    event_type = event.event_type.strip()
    normalized_type = event_type.casefold().replace('-', '_').replace(' ', '_')
    detail = f'{event.title} {event.summary}'
    all_text = f'{event_type} {detail}'
    aliases = {
        'funding', 'investment', 'fundraising', 'ipo', 'listing', 'new_business',
        'new_business_launch', 'new_service', 'new_service_launch', 'new_product',
        'new_product_launch', 'm_and_a', 'management', 'organization_change',
        'market_expansion', 'overseas_expansion', 'hiring_expansion', 'partnership',
        'event', 'seminar', 'webinar', 'facility_opening', 'store_renovation',
        'anniversary', 'rebrand', 'ci_vi_change', 'mvv_change', 'site_renewal',
        'corporate_site_renewal',
    }
    canonical: list[str] = ([_EVENT_TYPE_ALIASES.get(normalized_type, normalized_type)]
                            if normalized_type in aliases else [])
    growth: set[str] = set()
    strong_communication: set[str] = set()
    medium_communication: set[str] = set()

    # Resolve mutually exclusive/negative contexts before generic service words.
    is_partnership = bool(_PARTNERSHIP.search(all_text))
    is_campaign = bool(_CAMPAIGN.search(all_text))
    is_seminar = bool(_EVENT_OR_SEMINAR.search(detail))
    is_price_update = bool(_PRICING_UPDATE.search(detail))
    is_feature_update = bool(_FEATURE_UPDATE.search(detail))
    explicit_offer_launch = bool(_SERVICE_LAUNCH.search(detail))
    if is_partnership:
        canonical.append('partnership')
        growth.add('market_or_footprint_expansion')
    if is_campaign:
        canonical.append('campaign')
    if is_seminar:
        canonical.append('event')
    if is_price_update:
        canonical.append('pricing_update')
    if is_feature_update:
        canonical.append('feature_update')

    if (re.fullmatch(r'(?:ipo|listing|public_listing)', event_type, re.I)
            or IPO_EVENT_TYPE.search(detail)):
        if 'ipo' not in canonical:
            canonical.append('ipo')

    site_renewal = bool(_SITE_RENEWAL.search(detail))
    new_offer = False
    identity_type = normalized_type in {
        'rebrand', 'ci_vi_change', 'mvv_change', 'site_renewal', 'corporate_site_renewal'}
    for signal_name, pattern in STRONG_COMMUNICATION_PATTERNS.items():
        if pattern.search(all_text) and (signal_name != 'identity_or_expression_change'
                or pattern.search(detail)
                or (pattern.search(event_type) and not (is_campaign or is_partnership or is_seminar))
                or (identity_type and not (is_campaign or is_partnership or is_seminar))):
            strong_communication.add(signal_name)
    if identity_type and not (is_campaign or is_partnership or is_seminar):
        strong_communication.add('identity_or_expression_change')
    for signal_name, pattern in GROWTH_SIGNAL_PATTERNS.items():
        if pattern.search(all_text):
            growth.add(signal_name)
    if normalized_type in {'funding', 'fundraising', 'investment', 'ipo', 'm_and_a'}:
        growth.add('capital_or_listing')
    if normalized_type in {'market_expansion', 'overseas_expansion', 'new_business_launch', 'partnership',
                           'facility_opening'}:
        growth.add('market_or_footprint_expansion')
    if normalized_type in {'hiring_expansion', 'organization_change', 'management'}:
        growth.add('team_or_organization_growth')

    for signal_name, pattern in MEDIUM_COMMUNICATION_PATTERNS.items():
        if pattern.search(all_text):
            medium_communication.add(signal_name)
    if explicit_offer_launch and not (is_seminar or is_partnership or is_price_update or is_feature_update):
        canonical.append('new_service')
        medium_communication.add('new_service_or_product')
        new_offer = True
    elif ('new_service' in canonical
          and not (is_seminar or is_partnership or is_price_update or is_feature_update)):
        medium_communication.add('new_service_or_product')
        new_offer = True
    if 'new_business' in canonical or 'new_business_or_market_entry' in medium_communication:
        canonical.append('new_business')
        new_offer = True
    if 'new_brand_or_company' in medium_communication:
        canonical.append('new_brand')
    if site_renewal:
        canonical.append('site_renewal')
        strong_communication.add('identity_or_expression_change')
    if 'identity_or_expression_change' in strong_communication:
        canonical.append('rebrand')
    if (new_offer and not (is_seminar or is_partnership or is_price_update or is_feature_update)
            and COMPLEX_SERVICE_CONTEXT.search(detail) and event.strength > 0):
        strong_communication.add('complex_service')
    if is_price_update or is_feature_update or is_campaign or is_seminar or is_partnership:
        if is_price_update or is_feature_update or is_partnership or is_seminar:
            canonical = [item for item in canonical if item != 'new_service']
        medium_communication.discard('new_service_or_product')
        medium_communication.discard('complex_service')
        medium_communication.discard('new_business_or_market_entry')
        medium_communication.discard('new_brand_or_company')
        strong_communication.discard('complex_service')
        if is_campaign and not STRONG_COMMUNICATION_PATTERNS['identity_or_expression_change'].search(detail):
            canonical = [item for item in canonical if item not in {'rebrand', 'new_brand'}]
            strong_communication.discard('identity_or_expression_change')
    if (is_partnership or is_seminar or is_campaign) and not \
            STRONG_COMMUNICATION_PATTERNS['identity_or_expression_change'].search(detail):
        canonical = [item for item in canonical if item not in {'rebrand', 'site_renewal'}]
    if 'identity_or_expression_change' in strong_communication:
        canonical.append('rebrand')

    if not canonical:
        canonical.append('other')
    return EventSemantics(tuple(dict.fromkeys(canonical)), tuple(sorted(growth)),
                          tuple(sorted(strong_communication)), tuple(sorted(medium_communication)))


def hard_filter(candidate: Opportunity) -> HardFilterResult:
    """Only reject from structured company identity facts, never incidental event text.

    The current inputs do not carry a verified, structured company business
    type.  Therefore this deterministic stage must abstain and leave the
    decision to Cheap WIN, which receives source-scoped evidence explicitly.
    """
    del candidate
    return HardFilterResult(False, (),
        '会社自体の業態を示す構造化された確認事実がないため、自動除外せずCheap WINへ渡します')


async def evaluate_cheap_win(client: LLMClient, settings: Settings, candidate: Opportunity,
                             vc_profiles: list[dict], filter_result: HardFilterResult) -> Generation[CheapWinOutput]:
    """Use local discovery facts/profile data only.  No web tool is enabled here."""
    input_data = {
        'candidate': candidate.model_dump(), 'vc_profiles': vc_profiles,
        'hard_filter': filter_result.model_dump(),
        'thresholds': {
            'drop_below': settings.win_pre_drop_threshold,
            'diagnostic_at_or_above': settings.win_pre_diagnostic_threshold,
        },
    }
    return await client.generate(model=settings.cheap_win_model, instructions=client.prompt('cheap_win'),
        input_text=json.dumps(input_data, ensure_ascii=False), output_type=CheapWinOutput,
        use_web_search=False, reasoning_effort=settings.cheap_win_reasoning_effort)


def _fundamental_event_gaps(opportunity: Opportunity | None) -> list[str]:
    """Check only Event identity/evidence needed to define an Opportunity.

    Buyer, budget, procurement, expression, and creative-partner gaps are not
    fundamental: Deep Research exists to investigate those.
    """
    if opportunity is None:
        return []
    if not opportunity.events:
        return ['企業変化Eventがない']
    gaps: list[str] = []
    for event in opportunity.events:
        if not event.company_name.strip():
            gaps.append('対象企業を特定できない')
        if not event.event_type.strip() or not event.title.strip():
            gaps.append('具体的な企業変化を特定できない')
        if not event.source_url or not event.source_type.strip() or not event.source_name.strip():
            gaps.append('Eventを裏付けるSourceを特定できない')
    return list(dict.fromkeys(gaps))


def researchable_event_signals(opportunity: Opportunity | None, settings: Settings) -> list[str]:
    """Interpret Event Type, title, summary, and strength as one Gate signal."""
    if opportunity is None:
        return []
    growth_signals: set[str] = set()
    medium_communication_signals: set[str] = set()
    strong_communication_signals: set[str] = set()
    high_strength_ipo_with_hypothesis: set[str] = set()
    for event in opportunity.events:
        semantics = classify_event_semantics(event)
        if event.strength <= 0:
            continue
        growth_signals.update(semantics.growth_signals)
        strong_communication_signals.update(
            f'{signal_name}:{"/".join(semantics.canonical_types)}:strength={event.strength:g}'
            for signal_name in semantics.strong_communication_signals)
        medium_communication_signals.update(
            f'{signal_name}:{"/".join(semantics.canonical_types)}:strength={event.strength:g}'
            for signal_name in semantics.medium_communication_signals)
        # A high-strength IPO can justify checking post-listing communication
        # needs. Generic VC funding items often have templated video hypotheses,
        # so strength plus that hypothesis must not turn funding alone into a
        # Research trigger.
        if ('ipo' in semantics.canonical_types
                and event.strength >= settings.gate_research_event_strength
                and COMMUNICATION_HYPOTHESIS.search(event.possible_video_need)):
            high_strength_ipo_with_hypothesis.add(
                f'high_strength_ipo_with_comms_hypothesis:{event.event_type}:{event.strength:g}')

    if strong_communication_signals:
        return sorted(strong_communication_signals)
    if high_strength_ipo_with_hypothesis:
        return sorted(high_strength_ipo_with_hypothesis)
    if medium_communication_signals and growth_signals:
        return sorted({f'growth_support:{signal}' for signal in growth_signals}
                      | medium_communication_signals)
    if len(medium_communication_signals) >= 2:
        return sorted(medium_communication_signals)
    return []


def gate_decision(result: CheapWinOutput, settings: Settings,
                  opportunity: Opportunity | None = None) -> tuple[str, str, list[str]]:
    """Allocate Research from Opportunity validity and Event value, not WIN certainty."""
    if result.hard_blocker:
        return 'drop', '明確なHard Blockerが確認された', []

    fundamental_gaps = _fundamental_event_gaps(opportunity)
    if fundamental_gaps:
        return 'hold', 'Opportunityの成立に必要なEvent情報が不足している', fundamental_gaps

    # The trial targets realistic, reachable buyers. A listed parent is held
    # for human review instead of consuming Deep Research capacity. The model
    # field is explicitly target-scoped, so subsidiaries/new operating
    # companies/independent brands can remain eligible.
    if result.listed_company:
        return 'hold', '上場企業本体は通常営業対象外のため、人間確認へ保留する', ['listed_company_parent']

    # Low WIN_PRE is a DROP only when WIN is assessed confidently and there is
    # supported negative evidence. A low score alone may reflect Unknowns.
    if (result.win_pre < settings.win_pre_drop_threshold
            and result.confidence == 'high' and result.risk_tags):
        return 'drop', '十分な確信のある低WIN_PREと確認済みの逆風がある', list(result.risk_tags)

    if (result.win_pre >= settings.win_pre_diagnostic_threshold
            and result.confidence in {'medium', 'high'}):
        return 'research', 'WINの直接EvidenceからDeep Researchの価値がある', []

    event_signals = researchable_event_signals(opportunity, settings)
    if event_signals:
        return 'research', 'Communication Triggerまたは強いIPO後の説明需要をDeep Researchで確認する価値がある', event_signals

    # confidence describes certainty in current WIN_PRE; it does not decide
    # whether Research is worthwhile. Unknown purchasing facts alone stay HOLD
    # only when neither the Event nor existing WIN evidence justifies Research.
    if result.confidence == 'high' and result.win_pre < settings.win_pre_drop_threshold:
        return 'hold', '低WIN_PREだがDROPに足る確認済み逆風はない', []
    return 'hold', 'Deep Researchを優先する強いEvent/Growth SignalまたはWIN Evidenceがない', []


def gate_status(result: CheapWinOutput, settings: Settings,
                opportunity: Opportunity | None = None) -> str:
    """Compatibility helper returning only the routing status."""
    return gate_decision(result, settings, opportunity)[0]
