import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from openai import AuthenticationError, RateLimitError, APITimeoutError, InternalServerError
from app.config import Settings
from app.llm import LLMClient, InvalidOutputError, extract_evidence
from app.schemas import ScoreOutput
from app.scoring import WEIGHTS


def response(text, output=None):
    return SimpleNamespace(output_text=text, status='completed', model_dump=lambda: {'output': output or []})


def sdk_for(side_effect):
    return SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=side_effect)))


def valid():
    return ScoreOutput(**dict.fromkeys(WEIGHTS, 8), reason='ok', risks=[]).model_dump_json()


def generate(client, web=False):
    return asyncio.run(client.generate(model='test', instructions='test', input_text='test',
                       output_type=ScoreOutput, use_web_search=web))


def test_json_retry():
    sdk = sdk_for([response('broken'), response(valid())])
    assert generate(LLMClient(Settings(), sdk)).value.video_need == 8
    assert sdk.responses.create.await_count == 2
    assert 'tools' not in sdk.responses.create.call_args.kwargs
    assert 'reasoning' not in sdk.responses.create.call_args.kwargs


def test_discovery_reasoning_reaches_api_on_retry():
    from app.discovery import discover_topic

    sdk = sdk_for([response('broken'), response('{"candidates": []}')])
    result = asyncio.run(discover_topic(LLMClient(Settings(), sdk), Settings(), 'test'))

    assert result == ([], set(), 0)
    assert sdk.responses.create.await_count == 2
    for call in sdk.responses.create.call_args_list:
        assert call.kwargs['reasoning'] == {'effort': 'xhigh'}
        assert call.kwargs['tools'] == [{'type': 'web_search'}]


def test_json_retry_bounded():
    sdk = sdk_for([response('{}')] * 3)
    with pytest.raises(InvalidOutputError):
        generate(LLMClient(Settings(), sdk))
    assert sdk.responses.create.await_count == 3


def test_auth_not_retried():
    error = AuthenticationError('secret error', response=httpx.Response(401,
        request=httpx.Request('POST', 'https://api.openai.com')), body=None)
    sdk = sdk_for(error)
    with pytest.raises(AuthenticationError):
        generate(LLMClient(Settings(), sdk))
    assert sdk.responses.create.await_count == 1


@pytest.mark.parametrize('status', [429, 500])
def test_transient_retry(monkeypatch, status):
    monkeypatch.setattr('app.llm.asyncio.sleep', AsyncMock())
    cls = RateLimitError if status == 429 else InternalServerError
    error = cls('temporary', response=httpx.Response(status,
        request=httpx.Request('POST', 'https://api.openai.com')), body=None)
    sdk = sdk_for(error)
    with pytest.raises(cls):
        generate(LLMClient(Settings(), sdk))
    assert sdk.responses.create.await_count == 4


def test_timeout_retry(monkeypatch):
    monkeypatch.setattr('app.llm.asyncio.sleep', AsyncMock())
    sdk = sdk_for([APITimeoutError(request=httpx.Request('POST', 'https://api.openai.com')), response(valid())])
    generate(LLMClient(Settings(), sdk))
    assert sdk.responses.create.await_count == 2


def test_web_evidence():
    output = [{'type': 'web_search_call', 'status': 'completed',
               'action': {'type': 'search', 'sources': [{'url': 'https://real.example/news'}]}},
              {'type': 'message', 'content': [{'type': 'output_text',
               'text': 'https://invented.example', 'annotations': [
                   {'type': 'url_citation', 'url': 'https://cited.example'}]}]}]
    sdk = sdk_for([response(valid(), output)])
    result = generate(LLMClient(Settings(), sdk), True)
    assert result.evidence_urls == {'https://real.example/news', 'https://cited.example'}
    assert sdk.responses.create.call_args.kwargs['include'] == ['web_search_call.action.sources']


def test_official_sdk_responses_wire_format():
    """Exercise the actual installed SDK with an in-memory HTTP transport."""
    import json
    from openai import AsyncOpenAI
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            'id': 'resp_test', 'object': 'response', 'created_at': 1, 'status': 'completed',
            'model': 'test', 'output': [{'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                'status': 'completed', 'content': [{'type': 'output_text', 'text': valid(), 'annotations': []}]}]})

    async def exercise():
        async with AsyncOpenAI(api_key='fake-test', max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as sdk:
            return await LLMClient(Settings(), sdk).generate(model='test', instructions='test',
                input_text='test', output_type=ScoreOutput, use_web_search=True)

    assert asyncio.run(exercise()).value.video_need == 8
    assert calls[0]['tools'] == [{'type': 'web_search'}]
    assert calls[0]['store'] is False
