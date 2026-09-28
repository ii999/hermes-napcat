import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from hermes_napcat.config import ClassifierSettings
from hermes_napcat.engagement import Candidate, ParticipationClassifier


@asynccontextmanager
async def endpoint(handler):
    app = web.Application()
    app.router.add_post('/v1/chat/completions', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f'http://127.0.0.1:{port}/v1'
    finally:
        await runner.cleanup()


async def test_classifier_request_is_tool_free_and_binds_the_target():
    captured = []
    async def handler(request):
        captured.append((dict(request.headers), await request.json()))
        result = {'respond': True, 'confidence': .95, 'target_message_id': '-12'}
        return web.json_response({'choices': [{'message': {'content': json.dumps(result)}}]})
    async with endpoint(handler) as base_url:
        classifier = ParticipationClassifier(ClassifierSettings(
            enabled=True, model='test-model', base_url=base_url), 'test-classifier-key')
        try:
            decision = await classifier.decide(Candidate('-12', '200', 'help me', True), 'history')
            assert decision.respond and decision.confidence == .95
            headers, body = captured[0]
            assert headers['Authorization'] == 'Bearer test-classifier-key'
            assert 'tools' not in body and body['response_format']['type'] == 'json_object'
            assert json.loads(body['messages'][1]['content'])['candidate']['user_id'] == '200'
        finally:
            await classifier.close()


@pytest.mark.parametrize('result', [
    {'respond': 'true', 'confidence': .95, 'target_message_id': '-12'},
    {'respond': True, 'confidence': True, 'target_message_id': '-12'},
    {'respond': True, 'confidence': float('nan'), 'target_message_id': '-12'},
    {'respond': True, 'confidence': .95, 'target_message_id': '999'},
])
async def test_classifier_invalid_decisions_fail_closed(result):
    async def handler(request):
        return web.json_response({'choices': [{'message': {'content': json.dumps(result)}}]})
    async with endpoint(handler) as base_url:
        classifier = ParticipationClassifier(ClassifierSettings(
            enabled=True, model='test-model', base_url=base_url))
        try:
            with pytest.raises(ValueError):
                await classifier.decide(Candidate('-12', '200', 'question', True), 'history')
        finally:
            await classifier.close()


@pytest.mark.parametrize('status,body', [(302, ''), (200, 'x' * 32769)])
async def test_classifier_does_not_follow_redirects_or_accept_unbounded_responses(status, body):
    calls = []
    async def handler(request):
        calls.append(1)
        return web.Response(status=status, text=body, headers={'Location': '/v1/chat/completions'})
    async with endpoint(handler) as base_url:
        classifier = ParticipationClassifier(ClassifierSettings(
            enabled=True, model='test-model', base_url=base_url))
        try:
            with pytest.raises(ValueError):
                await classifier.decide(Candidate('-12', '200', 'question', True), 'history')
            assert len(calls) == 1
        finally:
            await classifier.close()
