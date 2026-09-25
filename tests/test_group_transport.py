import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from hermes_napcat.transport import OneBotTransport
from test_group_context import config


class Frames:
    def __init__(self, payloads):
        self.payloads = payloads

    def __aiter__(self):
        return self.read()

    async def read(self):
        for payload in self.payloads:
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(payload))


@pytest.mark.parametrize('enabled', [True, False])
async def test_recall_delivery_requires_opt_in_and_transport_authentication(enabled):
    async def handler(event):
        pass
    settings = config(group_context={'enabled': enabled})
    transport = OneBotTransport(settings, handler)
    notice = {'post_type': 'notice', 'notice_type': 'group_recall', 'self_id': 100,
              'group_id': 300, 'message_id': 10}
    authenticated = asyncio.Event()
    pending = []
    await transport._receive(Frames([notice]), authenticated, pending)
    assert transport._queue.empty()
    assert len(pending) == int(enabled)
    authenticated.set()
    await transport._receive(Frames([notice]), authenticated, [])
    assert transport._queue.qsize() == int(enabled)
