"""The only OpenAI SDK boundary; bounded transport and JSON retries."""
import asyncio
from dataclasses import dataclass, field
import json
import logging
from collections import defaultdict
from typing import Any, Callable, Generic, TypeVar
from openai.types.responses import Response
from app.config import Settings

from openai import AsyncOpenAI, APIConnectionError, APIStatusError
from pydantic import BaseModel, ValidationError

T = TypeVar('T', bound=BaseModel)
log = logging.getLogger(__name__)


@dataclass
class Generation(Generic[T]):
    value: T
    evidence_urls: set[str]
    warnings: list[str] = field(default_factory=list)


class InvalidOutputError(ValueError):
    """Bounded validation summary after the configured JSON correction retries."""
    def __init__(self, message: str, *, validation_type: str = 'output_validation',
                 field_errors: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.validation_type = validation_type
        self.field_errors = field_errors or []


def extract_evidence(response: dict) -> set[str]:
    """Only trust tool metadata, never URLs appearing solely in generated text."""
    urls = set()
    for item in response.get('output') or []:
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'web_search_call' and item.get('status') == 'completed':
            action = item.get('action') or {}
            if not isinstance(action, dict):
                action = {}
            for source in action.get('sources') or []:
                if isinstance(source, dict) and source.get('url'):
                    urls.add(source['url'])
            if action.get('type') == 'open_page' and action.get('url'):
                urls.add(action['url'])
        elif item.get('type') == 'message':
            for content in item.get('content') or []:
                if not isinstance(content, dict):
                    continue
                for annotation in content.get('annotations') or []:
                    if (isinstance(annotation, dict)
                            and annotation.get('type') == 'url_citation'
                            and annotation.get('url')):
                        urls.add(annotation['url'])
    return urls


class LLMClient:
    def __init__(self, settings: Settings, sdk: AsyncOpenAI | None = None) -> None:
        self.settings = settings
        self.sdk = sdk or AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.openai_timeout_seconds, max_retries=0)
        self.request_count = 0
        self.web_search_request_count = 0
        self._usage: dict[str, dict[str, int | bool]] = defaultdict(
            lambda: {
                'generate_calls': 0, 'api_requests': 0,
                'validation_retries': 0, 'transient_retries': 0,
                'web_search_api_requests': 0,
                'input_tokens': 0, 'output_tokens': 0,
                'reasoning_tokens': 0, 'cached_input_tokens': 0,
                'usage_observed': False,
            })

    def prompt(self, name: str) -> str:
        directory = self.settings.prompts_dir
        return (directory / 'context.md').read_text(encoding='utf-8') + '\n\n' + (
            directory / f'{name}.md').read_text(encoding='utf-8')

    async def close(self) -> None:
        await self.sdk.close()

    async def _request(self, **kwargs: Any) -> Response:
        max_transport_retries = kwargs.pop('_max_transport_retries', 3)
        stage = kwargs.pop('_stage', 'unattributed')
        use_web_search = kwargs.pop('_use_web_search', False)
        for attempt in range(max_transport_retries + 1):
            try:
                self.request_count += 1
                self._usage[stage]['api_requests'] += 1
                if use_web_search:
                    self.web_search_request_count += 1
                    self._usage[stage]['web_search_api_requests'] += 1
                return await self.sdk.responses.create(**kwargs)
            except (APIConnectionError, APIStatusError) as exc:
                retryable = isinstance(exc, APIConnectionError) or exc.status_code == 429 or exc.status_code >= 500
                if not retryable or attempt == max_transport_retries:
                    raise
                self._usage[stage]['transient_retries'] += 1
                log.warning('OpenAI transient failure type=%s retry=%d', type(exc).__name__, attempt + 1)
                await asyncio.sleep(2 ** attempt)

    async def generate(self, *, model: str, instructions: str, input_text: str,
                       output_type: type[T], use_web_search: bool = False,
                       reasoning_effort: str | None = None,
                       max_validation_retries: int = 2,
                       stage: str = 'unattributed',
                       normalizer: Callable[[str], str] | None = None) -> Generation[T]:
        schema = json.dumps(output_type.model_json_schema(), ensure_ascii=False)
        instructions += '\nReturn only valid JSON matching this schema:\n' + schema
        evidence: set[str] = set()
        last_validation_type = 'output_validation'
        last_field_errors: list[dict[str, str]] = []
        attempts = max(1, max_validation_retries + 1)
        self._usage[stage]['generate_calls'] += 1
        for attempt in range(attempts):
            kwargs = dict(model=model, instructions=instructions, input=input_text, store=False)
            if reasoning_effort is not None:
                kwargs['reasoning'] = {'effort': reasoning_effort}
            if use_web_search:
                kwargs.update(tools=[{'type': 'web_search'}], tool_choice='required',
                              include=['web_search_call.action.sources'])
            # Web Search requests can spend most of the timeout budget doing
            # server-side search/reasoning. A second attempt is useful, but
            # repeating the full 3-retry transport policy makes one topic
            # block the entire daily run for many minutes.
            response = await self._request(
                _max_transport_retries=1 if use_web_search else 3,
                _stage=stage, _use_web_search=use_web_search, **kwargs)
            response_data = response.model_dump()
            evidence.update(extract_evidence(response_data))
            _record_usage(self._usage[stage], response_data.get('usage'))
            try:
                if response.status != 'completed':
                    raise ValueError('Response not completed')
                output_text = normalizer(response.output_text) if normalizer else response.output_text
                value = output_type.model_validate_json(output_text)
                return Generation(value, evidence)
            except ValidationError as exc:
                last_validation_type = 'schema_validation'
                last_field_errors = _safe_validation_errors(exc)
                if attempt == attempts - 1:
                    raise InvalidOutputError(
                        f'Invalid model output after {attempts} attempts',
                        validation_type=last_validation_type, field_errors=last_field_errors) from None
                log.warning('OpenAI invalid JSON/schema retry=%d', attempt + 1)
                self._usage[stage]['validation_retries'] += 1
                instructions += '\nPrevious output was invalid. Return a complete JSON object matching the schema exactly.'
            except ValueError:
                last_validation_type = 'output_validation'
                last_field_errors = []
                if attempt == attempts - 1:
                    raise InvalidOutputError(
                        f'Invalid model output after {attempts} attempts',
                        validation_type=last_validation_type) from None
                log.warning('OpenAI invalid JSON/schema retry=%d', attempt + 1)
                self._usage[stage]['validation_retries'] += 1
                instructions += '\nPrevious output was invalid. Return a complete JSON object matching the schema exactly.'
        raise AssertionError('unreachable')

    def observability_snapshot(self) -> dict[str, dict[str, int | bool]]:
        return {stage: dict(values) for stage, values in self._usage.items()}


def _record_usage(bucket: dict[str, int | bool], usage: Any) -> None:
    if not usage:
        return
    if hasattr(usage, 'model_dump'):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        return
    bucket['usage_observed'] = True
    bucket['input_tokens'] += int(usage.get('input_tokens') or 0)
    bucket['output_tokens'] += int(usage.get('output_tokens') or 0)
    output_details = usage.get('output_tokens_details') or {}
    input_details = usage.get('input_tokens_details') or {}
    bucket['reasoning_tokens'] += int(output_details.get('reasoning_tokens') or 0)
    bucket['cached_input_tokens'] += int(input_details.get('cached_tokens') or 0)


def _safe_validation_errors(exc: ValidationError, limit: int = 8) -> list[dict[str, str]]:
    """Retain field/type/expected-form only; never retain model output values."""
    summaries: list[dict[str, str]] = []
    for error in exc.errors(include_input=False, include_context=False)[:limit]:
        location = '.'.join(str(part) for part in error.get('loc', ())) or '$'
        summaries.append({'field': location[:120], 'type': str(error.get('type', 'validation_error'))[:80],
                          'expected': str(error.get('msg', 'invalid value'))[:160]})
    return summaries
