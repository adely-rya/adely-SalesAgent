"""The only OpenAI SDK boundary; bounded transport and JSON retries."""
import asyncio
from dataclasses import dataclass
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


class InvalidOutputError(ValueError):
    pass


def extract_evidence(response: dict) -> set[str]:
    """Only trust tool metadata, never URLs appearing solely in generated text."""
    urls = set()
    for item in response.get('output', []):
        if item.get('type') == 'web_search_call' and item.get('status') == 'completed':
            action = item.get('action', {})
            for source in action.get('sources', []):
                if source.get('url'):
                    urls.add(source['url'])
            if action.get('type') == 'open_page' and action.get('url'):
                urls.add(action['url'])
        elif item.get('type') == 'message':
            for content in item.get('content', []):
                for annotation in content.get('annotations', []):
                    if annotation.get('type') == 'url_citation' and annotation.get('url'):
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
        for attempt in range(4):  # initial attempt plus at most 3 transport retries
            try:
                return await self.sdk.responses.create(**kwargs)
            except (APIConnectionError, APIStatusError) as exc:
                retryable = isinstance(exc, APIConnectionError) or exc.status_code == 429 or exc.status_code >= 500
                if not retryable or attempt == 3:
                    raise
                log.warning('OpenAI transient failure type=%s retry=%d', type(exc).__name__, attempt + 1)
                await asyncio.sleep(2 ** attempt)

    async def generate(self, *, model: str, instructions: str, input_text: str,
                       output_type: type[T], use_web_search: bool = False,
                       reasoning_effort: str | None = None) -> Generation[T]:
        schema = json.dumps(output_type.model_json_schema(), ensure_ascii=False)
        instructions += '\nReturn only valid JSON matching this schema:\n' + schema
        evidence: set[str] = set()
        for attempt in range(3):
            kwargs = dict(model=model, instructions=instructions, input=input_text, store=False)
            if reasoning_effort is not None:
                kwargs['reasoning'] = {'effort': reasoning_effort}
            if use_web_search:
                kwargs.update(tools=[{'type': 'web_search'}], tool_choice='required',
                              include=['web_search_call.action.sources'])
            response = await self._request(**kwargs)
            evidence.update(extract_evidence(response.model_dump()))
            try:
                if response.status != 'completed':
                    raise ValueError('Response not completed')
                value = output_type.model_validate_json(response.output_text)
                return Generation(value, evidence)
            except (ValidationError, ValueError):
                if attempt == 2:
                    raise InvalidOutputError('Invalid model output after 3 attempts') from None
                log.warning('OpenAI invalid JSON/schema retry=%d', attempt + 1)
                instructions += '\nPrevious output was invalid. Return a complete JSON object matching the schema exactly.'
        raise AssertionError('unreachable')
