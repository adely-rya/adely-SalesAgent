"""The only OpenAI SDK boundary; bounded transport and JSON retries."""
import asyncio
from dataclasses import dataclass, field
import json
import logging
from typing import Any, Generic, TypeVar
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

    def prompt(self, name: str) -> str:
        directory = self.settings.prompts_dir
        return (directory / 'context.md').read_text(encoding='utf-8') + '\n\n' + (
            directory / f'{name}.md').read_text(encoding='utf-8')

    async def close(self) -> None:
        await self.sdk.close()

    async def _request(self, **kwargs: Any) -> Response:
        max_transport_retries = kwargs.pop('_max_transport_retries', 3)
        for attempt in range(max_transport_retries + 1):
            try:
                return await self.sdk.responses.create(**kwargs)
            except (APIConnectionError, APIStatusError) as exc:
                retryable = isinstance(exc, APIConnectionError) or exc.status_code == 429 or exc.status_code >= 500
                if not retryable or attempt == max_transport_retries:
                    raise
                log.warning('OpenAI transient failure type=%s retry=%d', type(exc).__name__, attempt + 1)
                await asyncio.sleep(2 ** attempt)

    async def generate(self, *, model: str, instructions: str, input_text: str,
                       output_type: type[T], use_web_search: bool = False,
                       reasoning_effort: str | None = None) -> Generation[T]:
        schema = json.dumps(output_type.model_json_schema(), ensure_ascii=False)
        instructions += '\nReturn only valid JSON matching this schema:\n' + schema
        evidence: set[str] = set()
        last_validation_type = 'output_validation'
        last_field_errors: list[dict[str, str]] = []
        for attempt in range(3):
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
                _max_transport_retries=1 if use_web_search else 3, **kwargs)
            evidence.update(extract_evidence(response.model_dump()))
            try:
                if response.status != 'completed':
                    raise ValueError('Response not completed')
                value = output_type.model_validate_json(response.output_text)
                return Generation(value, evidence)
            except ValidationError as exc:
                last_validation_type = 'schema_validation'
                last_field_errors = _safe_validation_errors(exc)
                if attempt == 2:
                    raise InvalidOutputError('Invalid model output after 3 attempts',
                        validation_type=last_validation_type, field_errors=last_field_errors) from None
                log.warning('OpenAI invalid JSON/schema retry=%d', attempt + 1)
                instructions += '\nPrevious output was invalid. Return a complete JSON object matching the schema exactly.'
            except ValueError:
                last_validation_type = 'output_validation'
                last_field_errors = []
                if attempt == 2:
                    raise InvalidOutputError('Invalid model output after 3 attempts',
                        validation_type=last_validation_type) from None
                log.warning('OpenAI invalid JSON/schema retry=%d', attempt + 1)
                instructions += '\nPrevious output was invalid. Return a complete JSON object matching the schema exactly.'
        raise AssertionError('unreachable')


def _safe_validation_errors(exc: ValidationError, limit: int = 8) -> list[dict[str, str]]:
    """Retain field/type/expected-form only; never retain model output values."""
    summaries: list[dict[str, str]] = []
    for error in exc.errors(include_input=False, include_context=False)[:limit]:
        location = '.'.join(str(part) for part in error.get('loc', ())) or '$'
        summaries.append({'field': location[:120], 'type': str(error.get('type', 'validation_error'))[:80],
                          'expected': str(error.get('msg', 'invalid value'))[:160]})
    return summaries
