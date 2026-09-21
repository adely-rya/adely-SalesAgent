"""Expensive, structured research used only after the cheap WIN gate passes."""
from __future__ import annotations

import json
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
        return exc.category, {'error_type': type(exc).__name__, **exc.details}
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
                          'field_errors': fields}
    if isinstance(exc, (APITimeoutError, TimeoutError)):
        return 'diagnostic_timeout', {'error_type': type(exc).__name__}
    if isinstance(exc, (APIConnectionError, APIStatusError)):
        return 'diagnostic_api_error', {'error_type': type(exc).__name__}
    if isinstance(exc, ValidationError):
        return 'diagnostic_schema_validation', {'error_type': type(exc).__name__}
    if isinstance(exc, ValueError):
        return 'diagnostic_unknown_error', {'error_type': type(exc).__name__,
                                            'validation_type': 'unclassified_value_error'}
    return 'diagnostic_unknown_error', {'error_type': type(exc).__name__}


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

    referenced: list[tuple[str, str, str]] = [
        (f'evidence[{index}].source_url', str(item.source_url), item.claim)
        for index, item in enumerate(result.value.evidence) if item.source_url is not None]
    referenced.extend((f'current_expression.assets[{index}].url', str(asset.url), asset.observation)
                      for index, asset in enumerate(result.value.current_expression.assets) if asset.url)
    if result.value.peer_gap is not None:
        referenced.extend((f'peer_gap.peers[{index}].source_url', reference.source_url, reference.comparison)
                          for index, reference in enumerate(result.value.peer_gap.peers)
                          if reference.source_url)
    rejected_reference = next(((field, url, claim) for field, url, claim in referenced
                               if canonical_url(url) not in trusted_urls), None)
    if rejected_reference is not None:
        field, url, claim = rejected_reference
        raise DiagnosticFailure('diagnostic_untrusted_evidence_url',
            'Diagnostic output referenced a URL not present in tool or input evidence',
            details={'rejected_url': url, 'field': field, 'claim': claim[:240],
                     'validation_type': 'url_not_in_trusted_evidence',
                     'allowed_evidence_source_count': len(trusted_urls)})
    return result


def diagnostic_context(value: DiagnosticOutput) -> dict:
    """The bounded research payload passed forward to Scoring and Trace."""
    return {
        'current_expression': value.current_expression.model_dump(mode='json'),
        'expression_debt': value.expression_debt.model_dump(mode='json'),
        'peer_gap': value.peer_gap.model_dump(mode='json') if value.peer_gap else None,
        'creative_lock_in': value.creative_lock_in.model_dump(mode='json'),
        'evidence': [item.model_dump(mode='json') for item in value.evidence],
    }
