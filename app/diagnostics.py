"""Expensive, structured research used only after the cheap WIN gate passes."""
from __future__ import annotations

import json
import re
import traceback
from urllib.parse import urlsplit, urlunsplit

from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError

from app.config import Settings
from app.deduplication import canonical_url
from app.llm import Generation, InvalidOutputError, LLMClient
from app.schemas import DiagnosticOutput
from app.domain import Opportunity


class DiagnosticFailure(Exception):
    """Safe, categorized failure for Research; details exclude model/API bodies."""
    def __init__(self, category: str, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.details = details or {}


def classify_diagnostic_failure(exc: Exception) -> tuple[str, dict]:
    """Map Research exceptions to stable operational categories."""
    if isinstance(exc, DiagnosticFailure):
        return exc.category, {'error_type': type(exc).__name__,
                              'message': safe_exception_message(exc), **exc.details}
    if isinstance(exc, InvalidOutputError):
        fields = exc.field_errors[:8]
        if any(item.get('field', '').split('.', 1)[0] == 'evidence'
               and item.get('type') in {'missing', 'too_short'} for item in fields):
            category = 'diagnostic_missing_evidence'
        elif exc.validation_type == 'schema_validation':
            category = 'diagnostic_schema_validation'
        else:
            category = 'diagnostic_output_validation'
        return category, {'error_type': type(exc).__name__, 'validation_type': exc.validation_type,
                          'field_errors': fields, 'message': safe_exception_message(exc)}
    if isinstance(exc, (APITimeoutError, TimeoutError)):
        return 'diagnostic_timeout', {'error_type': type(exc).__name__,
                                      'message': safe_exception_message(exc)}
    if isinstance(exc, (APIConnectionError, APIStatusError)):
        return 'diagnostic_api_error', {'error_type': type(exc).__name__,
                                        'message': safe_exception_message(exc)}
    if isinstance(exc, ValidationError):
        return 'diagnostic_schema_validation', {'error_type': type(exc).__name__,
                                                'message': safe_exception_message(exc)}
    if isinstance(exc, ValueError):
        return 'diagnostic_unknown_error', {'error_type': type(exc).__name__,
                                            'validation_type': 'unclassified_value_error',
                                            'message': safe_exception_message(exc)}
    return 'diagnostic_unknown_error', {'error_type': type(exc).__name__,
                                       'message': safe_exception_message(exc)}


def safe_exception_message(exc: Exception) -> str:
    """Keep useful exception diagnostics without persisting credentials."""
    message = _redact_exception_text(str(exc).replace('\n', ' ').strip())
    return message[:500] or type(exc).__name__


def safe_exception_traceback(exc: Exception) -> str:
    """Return a development traceback with credential-like strings redacted."""
    return _redact_exception_text(traceback.format_exc())[:4000]


def _redact_exception_text(message: str) -> str:
    message = re.sub(r'(?i)bearer\s+[A-Za-z0-9._-]+', 'Bearer [REDACTED]', message)
    message = re.sub(r'(?i)(?:sk|rk)-[A-Za-z0-9_-]{8,}', '[REDACTED]', message)
    message = re.sub(r'https://discord(?:app)?\.com/api/webhooks/\S+', '[REDACTED_WEBHOOK]', message)
    message = re.sub(r'(?i)(api[-_ ]?key|authorization|webhook)\s*[:=]\s*\S+', r'\1=[REDACTED]', message)
    return message


def safe_diagnostic_url(url: str) -> str:
    """Keep public host/path for diagnosis, while removing query and user info."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))


async def run_diagnostic_research(client: LLMClient, settings: Settings, candidate: Opportunity,
                                  win_pre: dict, vc_profiles: list[dict]) -> Generation[DiagnosticOutput]:
    """Research current expression, debt, optional peer gap, and creative lock-in in one bounded call."""
    try:
        result = await client.generate(model=settings.diagnostic_model, instructions=client.prompt('diagnostic'),
            input_text=json.dumps({
                'candidate': candidate.model_dump(), 'win_pre': win_pre, 'vc_profiles': vc_profiles,
                'peer_research_enabled': settings.peer_research_enabled,
            }, ensure_ascii=False), output_type=DiagnosticOutput, use_web_search=settings.diagnostic_web_search,
            reasoning_effort=settings.diagnostic_reasoning_effort)
    except InvalidOutputError as exc:
        category, details = classify_diagnostic_failure(exc)
        raise DiagnosticFailure(category, 'Diagnostic model output failed validation', details=details) from exc
    except (APITimeoutError, TimeoutError) as exc:
        raise DiagnosticFailure('diagnostic_timeout', 'Diagnostic API request timed out',
                                details={'error_type': type(exc).__name__}) from exc
    except (APIConnectionError, APIStatusError) as exc:
        raise DiagnosticFailure('diagnostic_api_error', 'Diagnostic API request failed',
                                details={'error_type': type(exc).__name__}) from exc
    trusted_urls = {canonical_url(url) for url in result.evidence_urls}
    trusted_urls.update(canonical_url(str(event.source_url)) for event in candidate.events)
    trusted_urls.update(canonical_url(str(event.company_website)) for event in candidate.events
                        if event.company_website)
    trusted_urls.update(canonical_url(str(evidence.source_url))
                        for event in candidate.events for evidence in event.evidence)
    trusted_urls.update(canonical_url(str(fact.source_url))
                        for event in candidate.events for fact in event.research_facts)
    for profile in vc_profiles:
        trusted_urls.update(canonical_url(str(url)) for url in profile.get('evidence', []) if url)

    value = result.value
    warnings: list[str] = []
    evidence = []
    for index, item in enumerate(value.evidence):
        if item.source_url and canonical_url(str(item.source_url)) not in trusted_urls:
            warnings.append(f'untrusted evidence URL removed: evidence[{index}].source_url')
            evidence.append(item.model_copy(update={'source_url': None, 'evidence_type': 'unknown'}))
        else:
            evidence.append(item)

    assets = []
    unknowns = list(value.current_expression.unknowns)
    for index, asset in enumerate(value.current_expression.assets):
        if asset.url and canonical_url(asset.url) not in trusted_urls:
            warnings.append(f'untrusted evidence URL removed: current_expression.assets[{index}].url')
            if len(unknowns) < 6:
                unknowns.append('一部のアセットURLは検索結果または入力Evidenceで検証できず、確認済み事実として扱わない')
            assets.append(asset.model_copy(update={'url': None, 'evidence_confidence': 'low'}))
        else:
            assets.append(asset)

    peer_gap = value.peer_gap
    if peer_gap is not None:
        peers = []
        for index, reference in enumerate(peer_gap.peers):
            if reference.source_url and canonical_url(reference.source_url) not in trusted_urls:
                warnings.append(f'untrusted evidence URL removed: peer_gap.peers[{index}].source_url')
                continue
            peers.append(reference)
        peer_gap = peer_gap.model_copy(update={'peers': peers}) if peers else None

    sanitized = value.model_copy(update={
        'evidence': evidence,
        'current_expression': value.current_expression.model_copy(update={
            'assets': assets, 'unknowns': unknowns}),
        'peer_gap': peer_gap,
    })
    return Generation(sanitized, result.evidence_urls, warnings)


def diagnostic_context(value: DiagnosticOutput) -> dict:
    """The bounded research payload passed forward to Scoring and Trace."""
    return {
        'current_expression': value.current_expression.model_dump(mode='json'),
        'expression_debt': value.expression_debt.model_dump(mode='json'),
        'peer_gap': value.peer_gap.model_dump(mode='json') if value.peer_gap else None,
        'creative_lock_in': value.creative_lock_in.model_dump(mode='json'),
        'evidence': [item.model_dump(mode='json') for item in value.evidence],
    }
