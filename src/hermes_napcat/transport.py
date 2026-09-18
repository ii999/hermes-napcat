"""Full-duplex OneBot v11 transport with bounded admission and correlated actions."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import web

from .config import Settings, numeric_id

log = logging.getLogger(__name__)
EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class OneBotError(RuntimeError):
    pass


class NotConnected(OneBotError):
    """No write was attempted."""


class DeliveryUncertain(OneBotError):
    """A write may have reached QQ; callers must not retry the action automatically."""


class ActionError(OneBotError):
    def __init__(self, action: str, retcode: Any):
        super().__init__(f"OneBot rejected {action}: retcode={retcode!r}")
        self.retcode = retcode


class IdentityError(OneBotError):
    pass


@dataclass
class Stats:
    connections: int = 0
    events: int = 0
    dropped: int = 0
    malformed: int = 0
    responses: int = 0
    late_responses: int = 0
    handler_errors: int = 0


class OneBotTransport:
    def __init__(self, config: Settings, handler: EventHandler):
        self.config = config
        self.handler = handler
        self.stats = Stats()
        self.ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._ws: Any = None
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._queue: asyncio.Queue = asyncio.Queue(config.event_queue_size)
        self._workers: list[asyncio.Task] = []
        self._supervisor: asyncio.Task | None = None
        self._reverse_handlers: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self._attach_lock = asyncio.Lock()
        self._epoch = 0
        self._first: asyncio.Future | None = None
        self._chat_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.running = False

    @property
    def connected(self) -> bool:
        return self.ready.is_set() and self._ws is not None and not self._ws.closed

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._stop.clear()
        self._workers = [asyncio.create_task(self._worker(), name=f"napcat-event-{i}")
                         for i in range(self.config.event_workers)]
        try:
            if self.config.mode == "forward":
                self._session = aiohttp.ClientSession(trust_env=False)
                self._first = asyncio.get_running_loop().create_future()
                self._supervisor = asyncio.create_task(self._forward_loop(), name="napcat-connection")
                async with asyncio.timeout(self.config.connect_timeout + self.config.request_timeout):
                    await asyncio.shield(self._first)
            else:
                app = web.Application(client_max_size=self.config.ws_max_bytes)
                app.router.add_get(self.config.ws_path, self._reverse)
                self._runner = web.AppRunner(app, access_log=None)
                await self._runner.setup()
                site = web.TCPSite(self._runner, self.config.listen_host, self.config.listen_port)
                await site.start()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        self.running = False
        self._stop.set()
        self.ready.clear()
        self._fail_pending()
        if self._ws is not None:
            await self._ws.close()
        tasks = [t for t in [self._supervisor, *self._workers, *self._reverse_handlers]
                 if t is not None and t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._first is not None:
            if not self._first.done():
                self._first.cancel()
            elif not self._first.cancelled():
                self._first.exception()  # consume a startup failure if caller was cancelled
        self._ws = None
        self._workers.clear()
        self._reverse_handlers.clear()
        self._supervisor = None
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    async def _forward_loop(self) -> None:
        delay = self.config.reconnect_min
        while not self._stop.is_set():
            try:
                assert self._session is not None
                async with asyncio.timeout(self.config.connect_timeout):
                    ws = await self._session.ws_connect(
                        self.config.ws_url,
                        headers={"Authorization": f"Bearer {self.config.token.get_secret_value()}"},
                        heartbeat=30, max_msg_size=self.config.ws_max_bytes,
                        compress=0,
                    )
                await self._attach(ws)
                delay = self.config.reconnect_min
            except asyncio.CancelledError:
                raise
            except IdentityError:
                log.error("NapCat account does not match configured self_id; reconnect stopped")
                if self._first and not self._first.done():
                    self._first.set_exception(IdentityError("NapCat self_id mismatch"))
                self.running = False
                return
            except Exception as exc:
                # aiohttp exception strings can contain endpoint URLs. Never print credentials/URLs.
                log.warning("NapCat WebSocket unavailable (%s)", type(exc).__name__)
            if self._stop.is_set():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), delay + random.uniform(0, delay / 4))
            delay = min(delay * 2, self.config.reconnect_max)

    async def _reverse(self, request: web.Request) -> web.StreamResponse:
        expected = f"Bearer {self.config.token.get_secret_value()}"
        # Header-only auth prevents tokens entering proxy query-string logs.
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return web.Response(status=401, text="Unauthorized")
        if request.headers.get("X-Self-ID", self.config.self_id) != self.config.self_id:
            return web.Response(status=403, text="Account mismatch")
        role = request.headers.get("X-Client-Role", "Universal").lower()
        if role != "universal":
            return web.Response(status=400, text="Universal reverse WebSocket required")
        async with self._attach_lock:
            if self._ws is not None:
                return web.Response(status=409, text="One account connection is already active")
            ws = web.WebSocketResponse(heartbeat=30, max_msg_size=self.config.ws_max_bytes,
                                       compress=False)
            await ws.prepare(request)
            # Reserve synchronously before releasing the admission lock.
            self._ws = ws
        task = asyncio.current_task()
        self._reverse_handlers.add(task)
        try:
            await self._attach(ws)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Reverse OneBot session closed (%s)", type(exc).__name__)
        finally:
            self._reverse_handlers.discard(task)
        return ws

    async def _attach(self, ws) -> None:
        self._ws = ws
        self._epoch += 1
        self.ready.clear()
        preauth: list[dict] = []
        validated = asyncio.Event()
        reader = asyncio.create_task(self._receive(ws, validated, preauth), name="napcat-reader")
        try:
            identity = await self.call("get_login_info", {}, _handshake=True)
            try:
                matches = isinstance(identity, dict) and numeric_id(identity.get("user_id")) == self.config.self_id
            except ValueError:
                matches = False
            if not matches:
                raise IdentityError("NapCat self_id mismatch")
            validated.set()
            self.ready.set()
            self.stats.connections += 1
            for event in preauth:
                self._enqueue(event)
            preauth.clear()
            if self._first and not self._first.done():
                self._first.set_result(None)
            log.info("Authenticated OneBot connection is ready")
            await reader
        finally:
            if self._ws is ws:
                self.ready.clear()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await ws.close()
            if self._ws is ws:
                self._ws = None
                self._fail_pending()

    async def _receive(self, ws, validated: asyncio.Event, preauth: list[dict]) -> None:
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                if message.type == aiohttp.WSMsgType.ERROR:
                    log.warning("OneBot frame error; closing connection")
                continue
            try:
                payload = json.loads(message.data)
            except (ValueError, TypeError, RecursionError):
                self.stats.malformed += 1
                continue
            if not isinstance(payload, dict):
                self.stats.malformed += 1
                continue
            # Responses are consumed here, never by event workers that can themselves call actions.
            if "echo" in payload and "post_type" not in payload:
                echo = payload.get("echo")
                future = self._pending.get(echo) if isinstance(echo, str) else None
                if future is not None and not future.done():
                    self.stats.responses += 1
                    future.set_result(payload)
                else:
                    self.stats.late_responses += 1
            elif payload.get("post_type") == "message":
                if validated.is_set():
                    self._enqueue(payload)
                elif len(preauth) < self.config.event_queue_size:
                    preauth.append(payload)
                else:
                    self.stats.dropped += 1
                    log.warning("Pre-auth event buffer full; message dropped")
        self._fail_pending()

    def _enqueue(self, event: dict) -> None:
        try:
            self._queue.put_nowait(event)
            self.stats.events += 1
        except asyncio.QueueFull:
            self.stats.dropped += 1
            log.warning("OneBot event queue full; message dropped (OneBot has no durable replay)")

    async def _worker(self) -> None:
        while True:
            event = await self._queue.get()
            # Serialize adapter admission per conversation, without blocking the WS response reader.
            kind = event.get("message_type")
            key = f"{kind}:{event.get('group_id') if kind == 'group' else event.get('user_id')}"
            lock, count = self._chat_locks.get(key, (asyncio.Lock(), 0))
            self._chat_locks[key] = (lock, count + 1)
            try:
                async with lock:
                    await self.handler(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats.handler_errors += 1
                log.exception("OneBot event handler failed")
            finally:
                _, count = self._chat_locks[key]
                if count == 1:
                    del self._chat_locks[key]
                else:
                    self._chat_locks[key] = (lock, count - 1)
                self._queue.task_done()

    async def call(self, action: str, params: dict[str, Any] | None = None, *,
                   _handshake: bool = False) -> Any:
        import uuid
        ws = self._ws
        if ws is None or ws.closed or (not _handshake and not self.ready.is_set()):
            raise NotConnected("OneBot is not ready; no action was written")
        echo = f"{self._epoch}:{uuid.uuid4().hex}"
        request = json.dumps({"action": action, "params": params or {}, "echo": echo},
                             ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(request.encode("utf-8")) > self.config.ws_max_bytes:
            raise OneBotError("outbound frame exceeds configured byte limit")
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        write_started = False
        try:
            async with asyncio.timeout(self.config.request_timeout):
                async with self._send_lock:
                    if self._ws is not ws or ws.closed:
                        raise NotConnected("OneBot changed before write; no action was written")
                    write_started = True
                    await ws.send_str(request)
                response = await future
            if response.get("status") != "ok" or type(response.get("retcode")) is not int or response["retcode"] != 0:
                # status=async / retcode=1 is acceptance, not completed delivery.
                raise ActionError(action, response.get("retcode"))
            return response.get("data")
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            if write_started:
                raise DeliveryUncertain("Delivery outcome unknown; verify before retrying") from exc
            raise NotConnected("OneBot action was not written") from exc
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DeliveryUncertain("Delivery outcome unknown; verify before retrying"))
