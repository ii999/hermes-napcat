from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from hermes_napcat.transport import ActionError, DeliveryUncertain, IdentityError, NotConnected, OneBotTransport

TOKEN = "test-onebot-secret-for-local-tests"


async def ignore(event):
    pass


async def wait_until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@asynccontextmanager
async def fake_napcat(callback=None, own_id=100):
    state = {"ws": None, "requests": [], "connections": 0}
    tasks = set()

    async def endpoint(request):
        assert request.headers.get("Authorization") == "Bearer " + TOKEN
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state["ws"] = ws
        state["connections"] += 1
        async for frame in ws:
            if frame.type != aiohttp.WSMsgType.TEXT:
                continue
            payload = json.loads(frame.data)
            state["requests"].append(payload)
            if payload["action"] == "get_login_info":
                await ws.send_json({"status": "ok", "retcode": 0,
                                    "data": {"user_id": own_id}, "echo": payload["echo"]})
            elif callback:
                task = asyncio.create_task(callback(ws, payload))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            else:
                await ws.send_json({"status": "ok", "retcode": 0,
                                    "data": payload["params"], "echo": payload["echo"]})
        return ws

    app = web.Application()
    app.router.add_get("/", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    state["url"] = f"ws://127.0.0.1:{port}"
    try:
        yield state
    finally:
        if state["ws"] is not None:
            await state["ws"].close()
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()


async def test_full_duplex_event_handler_can_call_action_without_deadlock(settings, raw_event):
    observed = asyncio.Future()
    async with fake_napcat() as server:
        async def handler(event):
            observed.set_result(await transport.call("get_group_info", {"group_id": 300}))
        transport = OneBotTransport(settings(ws_url=server["url"]), handler)
        try:
            await transport.start()
            assert transport.connected
            await server["ws"].send_json(raw_event())
            assert await asyncio.wait_for(observed, 1) == {"group_id": 300}
            assert transport.pending_count == 0
        finally:
            await transport.stop()
            await transport.stop()
        assert not transport.connected
        with pytest.raises(NotConnected):
            await transport.call("send_private_msg")


async def test_concurrent_out_of_order_echo(settings):
    async def callback(ws, request):
        n = request["params"]["n"]
        await asyncio.sleep((6 - n) * 0.008)
        await ws.send_json({"status": "ok", "retcode": 0, "data": n, "echo": request["echo"]})
    async with fake_napcat(callback) as server:
        transport = OneBotTransport(settings(ws_url=server["url"]), ignore)
        try:
            await transport.start()
            assert await asyncio.gather(*(transport.call("lookup", {"n": n}) for n in range(6))) == list(range(6))
            assert transport.pending_count == 0
        finally:
            await transport.stop()


async def test_timeout_does_not_retry_and_late_response_is_discarded(settings):
    async def callback(ws, request):
        await asyncio.sleep(0.12)
        await ws.send_json({"status": "ok", "retcode": 0, "data": {"message_id": 8}, "echo": request["echo"]})
    async with fake_napcat(callback) as server:
        transport = OneBotTransport(settings(ws_url=server["url"], request_timeout=0.04), ignore)
        try:
            await transport.start()
            with pytest.raises(DeliveryUncertain):
                await transport.call("send_private_msg")
            assert transport.pending_count == 0
            await wait_until(lambda: transport.stats.late_responses == 1)
            assert [p["action"] for p in server["requests"]].count("send_private_msg") == 1
        finally:
            await transport.stop()


async def test_disconnect_fails_pending_and_reconnects(settings):
    async def callback(ws, request):
        await ws.close()
    async with fake_napcat(callback) as server:
        transport = OneBotTransport(settings(ws_url=server["url"]), ignore)
        try:
            await transport.start()
            with pytest.raises(DeliveryUncertain):
                await transport.call("send_private_msg")
            await wait_until(lambda: transport.stats.connections >= 2)
            assert transport.connected and transport.pending_count == 0
        finally:
            await transport.stop()


@pytest.mark.parametrize("status,code", [("failed", 1400), ("async", 1), ("ok", True)])
async def test_only_completed_success_is_success(settings, status, code):
    async def callback(ws, request):
        await ws.send_json({"status": status, "retcode": code, "data": {}, "echo": request["echo"]})
    async with fake_napcat(callback) as server:
        transport = OneBotTransport(settings(ws_url=server["url"]), ignore)
        try:
            await transport.start()
            with pytest.raises(ActionError):
                await transport.call("send_private_msg")
        finally:
            await transport.stop()


async def test_wrong_qq_account_fails_startup_without_processing(settings):
    async with fake_napcat(own_id=999) as server:
        transport = OneBotTransport(settings(ws_url=server["url"]), ignore)
        with pytest.raises(IdentityError):
            await transport.start()
        assert not transport.running and not transport.connected
        assert not transport._workers


async def test_bounded_queue_does_not_block_response_processing(settings, raw_event):
    gate = asyncio.Event()
    async def handler(event):
        await gate.wait()
    async with fake_napcat() as server:
        transport = OneBotTransport(settings(ws_url=server["url"], event_queue_size=1, event_workers=1), handler)
        try:
            await transport.start()
            await server["ws"].send_str("{bad")
            await server["ws"].send_json([])
            for n in range(20):
                await server["ws"].send_json(raw_event(message_id=n))
            assert await transport.call("get_status", {"online": True}) == {"online": True}
            assert transport.stats.dropped > 0
            assert transport.stats.malformed == 2
        finally:
            gate.set()
            await transport.stop()


async def test_reverse_auth_identity_role_and_single_client(settings, unused_tcp_port):
    transport = OneBotTransport(settings(mode="reverse", listen_port=unused_tcp_port), ignore)
    await transport.start()
    url = f"http://127.0.0.1:{unused_tcp_port}/onebot/v11"
    headers = {"Authorization": "Bearer " + TOKEN, "X-Self-ID": "100", "X-Client-Role": "Universal"}
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(url) as response:
                assert response.status == 401
            async with client.get(url, headers={**headers, "X-Self-ID": "999"}) as response:
                assert response.status == 403
            async with client.get(url, headers={**headers, "X-Client-Role": "Event"}) as response:
                assert response.status == 400
            ws = await client.ws_connect(url, headers=headers)
            first = await ws.receive_json()
            assert first["action"] == "get_login_info"
            await ws.send_json({"status": "ok", "retcode": 0, "data": {"user_id": 100}, "echo": first["echo"]})
            await asyncio.wait_for(transport.ready.wait(), 1)
            async with client.get(url, headers=headers) as response:
                assert response.status == 409
            action = asyncio.create_task(transport.call("get_status"))
            payload = await ws.receive_json()
            await ws.send_json({"status": "ok", "retcode": 0, "data": {"online": True}, "echo": payload["echo"]})
            assert await action == {"online": True}
            await ws.close()
            await wait_until(lambda: not transport.connected)
    finally:
        await transport.stop()


async def test_same_chat_order_and_cross_chat_parallelism(settings, raw_event):
    entered = []
    gate = asyncio.Event()
    done = asyncio.Event()
    async def handler(event):
        entered.append(event["message_id"])
        if event["message_id"] == 1:
            await gate.wait()
        if event["message_id"] == 3:
            done.set()
    async with fake_napcat() as server:
        transport = OneBotTransport(settings(ws_url=server["url"], event_workers=3), handler)
        try:
            await transport.start()
            for n, user in [(1, 200), (2, 200), (3, 201)]:
                await server["ws"].send_json(raw_event(message_id=n, user_id=user))
            await asyncio.wait_for(done.wait(), 1)
            assert entered == [1, 3]
            gate.set()
            await wait_until(lambda: len(entered) == 3)
            assert entered == [1, 3, 2]
        finally:
            gate.set()
            await transport.stop()


async def test_oversize_incoming_frame_closes_socket_and_recovers(settings):
    async with fake_napcat() as server:
        transport = OneBotTransport(settings(ws_url=server["url"], ws_max_bytes=32768,
                                            media={"inline_max_bytes": 1024}), ignore)
        try:
            await transport.start()
            await server["ws"].send_str("x" * 40000)
            await wait_until(lambda: transport.stats.connections >= 2)
            assert transport.connected
        finally:
            await transport.stop()
