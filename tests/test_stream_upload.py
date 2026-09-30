"""Source-contract and loopback tests, not a real NapCat/QQ acceptance run."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from hermes_napcat import tools
from hermes_napcat.config import MediaSettings
from hermes_napcat.media import MediaError, MediaStore, inline_info
from hermes_napcat.protocol import Target
from hermes_napcat.stream_upload import StreamedMedia, StreamUploader
from hermes_napcat.transport import OneBotTransport, NotConnected
from test_adapter import make_adapter
from test_media import PNG
from test_media_upgrade import owned
from test_tools import bind_session
from test_transport import fake_napcat, ignore, wait_until


class UploadServer:
    """Match the inspected UploadFileStream.ts response envelope and reset behavior."""

    def __init__(self, *, corrupt=None, delay=0, fail_index=None, fail_send=False):
        self.files = {}
        self.complete = []
        self.corrupt, self.delay, self.fail_index, self.fail_send = corrupt, delay, fail_index, fail_send
        self.resets = []
        self.entered = asyncio.Event()

    async def __call__(self, ws, request):
        action, p = request['action'], request['params']
        if action == 'upload_file_stream':
            sid = p['stream_id']
            if p.get('reset'):
                self.resets.append(sid)
                self.files.pop(sid, None)
                await ws.send_json({'status': 'failed', 'retcode': 1200, 'echo': request['echo'],
                                    'data': {'type': 'error'}})
                return
            if p.get('is_complete'):
                state = self.files[sid]
                data = b''.join(state['chunks'])
                assert len(data) == state['size']
                assert hashlib.sha256(data).hexdigest() == state['hash']
                result = {'type': 'response', 'status': 'file_complete', 'stream_id': sid,
                          'received_chunks': len(state['chunks']), 'total_chunks': state['total'],
                          'file_size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                          'file_path': '/napcat/temp/' + state['name']}
                self.complete.append(data)
                if self.corrupt:
                    result.update(self.corrupt)
            else:
                self.entered.set()
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.fail_index is not None and p['chunk_index'] == self.fail_index:
                    await ws.send_json({'status': 'failed', 'retcode': 1404,
                                        'echo': request['echo'], 'data': None})
                    return
                state = self.files.setdefault(sid, {'chunks': [], 'size': p['file_size'],
                    'hash': p['expected_sha256'], 'total': p['total_chunks'], 'name': p['filename']})
                assert p['chunk_index'] == len(state['chunks'])
                state['chunks'].append(base64.b64decode(p['chunk_data'], validate=True))
                result = {'type': 'stream', 'status': 'chunk_received', 'stream_id': sid,
                          'received_chunks': len(state['chunks']), 'total_chunks': state['total']}
        elif action in ('send_private_msg', 'send_group_msg', 'send_group_forward_msg'):
            if self.fail_send:
                await ws.close()
                return
            result = {'message_id': 77}
        elif action in ('upload_group_file', 'upload_private_file'):
            result = {'file_id': 'file-77'}
        else:
            result = p
        await ws.send_json({'status': 'ok', 'retcode': 0, 'echo': request['echo'], 'data': result,
                            'stream': 'stream-action' if action == 'upload_file_stream' else 'normal-action'})


@asynccontextmanager
async def live_adapter(hermes_doubles, settings, tmp_path, *, callback=None, media=None, **kwargs):
    callback = callback or UploadServer()
    root = tmp_path / 'output'
    root.mkdir(exist_ok=True)
    options = {'inline_max_bytes': 1024, 'outbound_roots': [root],
               'streaming': {'chunk_bytes': 1024}}
    options.update(media or {})
    async with fake_napcat(callback) as server:
        adapter = make_adapter(hermes_doubles, settings, ws_url=server['url'], media=options,
                               qq_tools={'enabled': True}, **kwargs)
        adapter.media = MediaStore(adapter.settings.media, tmp_path / 'cache')
        adapter.transport = OneBotTransport(adapter.settings, adapter._receive)
        await adapter.transport.start()
        try:
            yield adapter, root, callback, server
        finally:
            await adapter.disconnect()


def test_new_limits_and_old_explicit_ws_limit(settings):
    config = settings()
    assert config.media.max_bytes == 32 * 1024**2
    assert config.media.inline_max_bytes == 10 * 1024**2
    assert config.ws_max_bytes == 16 * 1024**2
    assert config.media.streaming.max_bytes == 256 * 1024**2
    assert config.media.timeout == 60
    assert settings(ws_max_bytes=2 * 1024**2).ws_max_bytes == 2 * 1024**2


@pytest.mark.parametrize('prefix', ['base64://', 'data:image/png;base64,'])
def test_inline_import_and_cache_accounting(tmp_path, prefix):
    store = MediaStore(MediaSettings(), tmp_path / 'cache')
    value = PNG + b'x' * 100000  # Decode across multiple base64 blocks.
    result = store.import_inline(prefix + base64.b64encode(value).decode(), kind='image')
    assert result.mime == 'image/png' and result.path.read_bytes() == value
    assert result.size == len(value) == store._prune_and_usage()
    assert result.path.stat().st_mode & 0o777 == 0o600
    assert not list(store.root.glob('*.part'))


@pytest.mark.parametrize('payload', [
    'base64://', 'base64://SGVsbG8', 'base64://!!!!', 'base64://AB==',
    'base64://YWJj\nAAA', 'data:image/png,abc', 'data:image/png;base64,****',
    'data:image/png;charset=utf-8;base64,AAAA',
    'data:image/jpeg;base64,' + base64.b64encode(PNG).decode(),
    'base64://' + base64.b64encode(b'not an image').decode(),
    'base64://' + 'YQ==' + 'AAAA' * (64 * 1024 // 4),
])
def test_invalid_base64_fails_without_retaining_files(tmp_path, payload):
    store = MediaStore(MediaSettings(), tmp_path / 'cache')
    with pytest.raises(MediaError):
        store.import_inline(payload, kind='image')
    assert not list(store.root.iterdir())


def test_inline_limit_precedes_decode_and_shared_cache_import_obeys_quota(tmp_path):
    payload = 'base64://' + base64.b64encode(PNG + b'x' * 2000).decode()
    with pytest.raises(MediaError, match='byte limit'):
        inline_info(payload, 1024)
    store = MediaStore(MediaSettings(max_bytes=4096, cache_max_bytes=4096), tmp_path / 'cache')
    store.import_inline(payload, kind='image')
    with pytest.raises(MediaError, match='quota'):
        store.import_inline(payload, kind='image')
    assert len(list(store.root.iterdir())) == 1


@pytest.mark.parametrize('kind', ['image', 'audio', 'video', 'file'])
async def test_base64_tools_and_gateway_sources(monkeypatch, hermes_doubles, settings, tmp_path, kind):
    adapter = make_adapter(hermes_doubles, settings, qq_tools={'enabled': True})
    adapter.media = MediaStore(adapter.settings.media, tmp_path / 'cache')
    bind_session(monkeypatch, adapter, target='private:200')
    value = PNG + b'x' * 10000 if kind == 'image' else b'fixture media' * 1000
    source = 'base64://' + base64.b64encode(value).decode()
    result = json.loads(await tools.qq_send_media({'media_type': kind, 'source': source}))
    assert result['success']
    sent = adapter.transport.call.call_args.args[1]
    ref = sent['file'] if kind == 'file' else sent['message'][-1]['data']['file']
    assert base64.b64decode(ref.removeprefix('base64://')) == value
    if kind == 'file':
        assert sent['name'] == 'attachment.bin'
    else:
        assert adapter.transport.call.await_count == 1


async def test_base64_default_accepts_screenshot_above_old_inline_limit(hermes_doubles, settings, tmp_path):
    adapter = make_adapter(hermes_doubles, settings)
    adapter.media = MediaStore(adapter.settings.media, tmp_path / 'cache')
    source = 'data:image/png;base64,' + base64.b64encode(PNG + b'x' * 1024**2).decode()
    result = await adapter.send_image('private:200', source)
    assert result.success
    assert adapter.transport.call.call_args.args[0] == 'send_private_msg'
    assert adapter.transport.call.await_count == 1


async def test_base64_batch_budget_before_serialization_or_io(monkeypatch, hermes_doubles, settings, tmp_path):
    adapter = make_adapter(hermes_doubles, settings, qq_tools={'enabled': True}, media={
        'base64_batch_max_bytes': 1024})
    adapter.media = MediaStore(adapter.settings.media, tmp_path / 'cache')
    bind_session(monkeypatch, adapter)
    monkeypatch.setattr(tools, '_action_key', lambda *args: pytest.fail('key serialized before size check'))
    source = 'base64://' + base64.b64encode(PNG + b'x' * 600).decode()
    result = json.loads(await tools.qq_send_message({'images': [source, source]}))
    assert not result['success']
    adapter.transport.call.assert_not_called()
    assert not list(adapter.media.root.iterdir())


@pytest.mark.parametrize('kind', ['image', 'record', 'video', 'file'])
async def test_stream_upload_acknowledgements_hash_and_separate_qq_send(
    hermes_doubles, settings, tmp_path, kind,
):
    async with live_adapter(hermes_doubles, settings, tmp_path) as (adapter, root, remote, server):
        value = PNG + b'x' * 3500 if kind == 'image' else b'stream data' * 400
        path = root / ('report.png' if kind == 'image' else 'report.bin')
        path.write_bytes(value)
        method = {'image': adapter.send_image_file, 'record': adapter.send_voice,
                  'video': adapter.send_video, 'file': adapter.send_document}[kind]
        result = await method('private:200', str(path))
        assert result.success and remote.complete == [value]
        requests = server['requests'][1:]
        uploads = [r for r in requests if r['action'] == 'upload_file_stream']
        assert len(uploads) == (len(value) + 1023) // 1024 + 1
        assert uploads[-1]['params']['is_complete'] is True
        assert requests[-1]['action'] == ('upload_private_file' if kind == 'file' else 'send_private_msg')
        assert len({r['echo'] for r in requests}) == len(requests)
        assert not remote.resets
        assert not any('base64://' in json.dumps(r) for r in requests)
        assert all(len(json.dumps(r, ensure_ascii=False, separators=(',', ':')).encode())
                   <= adapter.settings.ws_max_bytes for r in requests)


async def test_small_explicit_ws_limit_uses_stream_instead_of_breaking_config(
    hermes_doubles, settings, tmp_path,
):
    async with live_adapter(hermes_doubles, settings, tmp_path, ws_max_bytes=1800,
                            media={'inline_max_bytes': 10 * 1024**2,
                                   'streaming': {'chunk_bytes': 256 * 1024}}) as (adapter, root, remote, server):
        path = root / 'test.png'
        path.write_bytes(PNG + b'x' * 4000)
        assert (await adapter.send_image_file('private:200', str(path))).success
        assert remote.complete == [path.read_bytes()]
        assert all(len(json.dumps(r, separators=(',', ':')).encode()) <= 1800 for r in server['requests'])


@pytest.mark.parametrize('change', [
    {'sha256': '0' * 64}, {'file_size': 0}, {'file_size': True}, {'stream_id': 'wrong'},
    {'file_path': '/etc/passwd'}, {'file_path': 'https://server/secret'},
    {'type': 'stream'}, {'status': 'file_created'}, {'received_chunks': 0},
])
async def test_bad_stream_completion_does_not_send(hermes_doubles, settings, tmp_path, change):
    async with live_adapter(hermes_doubles, settings, tmp_path,
                            callback=UploadServer(corrupt=change)) as (adapter, root, remote, server):
        path = root / 'image.png'
        path.write_bytes(PNG + b'x' * 2000)
        assert not (await adapter.send_image_file('private:200', str(path))).success
        assert not any(r['action'].startswith('send_') for r in server['requests'])
        assert len(remote.resets) == 1


async def test_failed_chunk_does_not_fallback_or_send(hermes_doubles, settings, tmp_path):
    async with live_adapter(hermes_doubles, settings, tmp_path,
                            callback=UploadServer(fail_index=1)) as (adapter, root, remote, server):
        path = root / 'image.png'
        path.write_bytes(PNG + b'x' * 4000)
        result = await adapter.send_image_file('private:200', str(path))
        assert not result.success and len(remote.resets) == 1
        actions = [r for r in server['requests'] if r['action'] != 'get_login_info']
        assert len(actions) == 3  # Two distinct chunks, one best-effort reset, no retransmission.


async def test_timeout_cancellation_and_upload_queue_are_bounded(hermes_doubles, settings, tmp_path):
    async with live_adapter(hermes_doubles, settings, tmp_path, callback=UploadServer(delay=0.5), media={
        'streaming': {'chunk_bytes': 1024, 'chunk_timeout': 0.03, 'max_pending': 1}
    }) as (adapter, root, remote, server):
        path = root / 'image.png'
        path.write_bytes(PNG + b'x' * 4000)
        uploader = StreamUploader(adapter.transport, adapter.settings)
        task = asyncio.create_task(uploader.upload(path, path.stat().st_size))
        await remote.entered.wait()
        with pytest.raises(MediaError, match='queue'):
            await uploader.upload(path, path.stat().st_size)
        with pytest.raises(MediaError, match='staging failed'):
            await task
        assert not uploader._tasks
        assert len(remote.resets) == 1 and not remote.complete
        assert adapter.transport.pending_count == 0


async def test_cancel_upload_and_close_do_not_send(hermes_doubles, settings, tmp_path):
    async with live_adapter(hermes_doubles, settings, tmp_path,
                            callback=UploadServer(delay=0.5)) as (adapter, root, remote, server):
        path = root / 'image.png'
        path.write_bytes(PNG + b'x' * 4000)
        task = asyncio.create_task(adapter.send_image_file('private:200', str(path)))
        await remote.entered.wait()
        await adapter._close_streams()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter.transport.pending_count == 0
        assert len(remote.resets) == 1
        assert not any(r['action'].startswith('send_') for r in server['requests'])


async def test_no_qq_send_retry_after_successful_stream_upload(hermes_doubles, settings, tmp_path):
    async with live_adapter(hermes_doubles, settings, tmp_path,
                            callback=UploadServer(fail_send=True)) as (adapter, root, remote, server):
        path = root / 'image.png'
        path.write_bytes(PNG + b'x' * 2000)
        result = await adapter.send_image_file('private:200', str(path))
        assert not result.success and result.raw_response['delivery_uncertain']
        assert len(remote.complete) == 1
        assert sum(r['action'] == 'send_private_msg' for r in server['requests']) == 1


async def test_staged_reference_rejected_after_reconnect_or_expiry(hermes_doubles, settings, tmp_path):
    async with live_adapter(hermes_doubles, settings, tmp_path) as (adapter, root, remote, server):
        path = root / 'image.png'
        path.write_bytes(PNG + b'x' * 2000)
        ref = await adapter.outbound_reference(str(path), kind='image')
        assert isinstance(ref, StreamedMedia)
        target = Target.parse('private:200')
        old = adapter.transport.connection_epoch
        await server['ws'].close()
        await wait_until(lambda: adapter.transport.connection_epoch > old and adapter.transport.connected)
        with pytest.raises(MediaError, match='connection'):
            await adapter.send_agent_parts(target, [{'type': 'image', 'data': {'file': ref}}])
        stale = StreamedMedia('file:///tmp/unused', adapter.transport.connection_epoch, time.monotonic() - 1)
        with pytest.raises(MediaError, match='retention'):
            stale.validate(adapter.transport)
        with pytest.raises(NotConnected):
            await adapter.transport.call('get_status', expected_epoch=old)


async def test_shared_mapping_precedes_stream_even_in_always_mode(hermes_doubles, settings, tmp_path):
    root = tmp_path / 'shared'
    root.mkdir()
    adapter = make_adapter(hermes_doubles, settings, media={
        'streaming': {'mode': 'always'}, 'shared_paths': [{'hermes': root, 'napcat': '/shared'}]})
    adapter.media = MediaStore(adapter.settings.media, tmp_path / 'cache')
    path = root / 'image.png'
    path.write_bytes(PNG + b'x' * 5000)
    assert (await adapter.send_image_file('private:200', str(path))).success
    adapter.transport.call.assert_awaited_once()
    assert adapter.transport.call.call_args.args[0] == 'send_private_msg'


async def test_url_and_base64_and_cached_reference_use_stream(
    monkeypatch, hermes_doubles, settings, raw_event, tmp_path,
):
    async with live_adapter(hermes_doubles, settings, tmp_path,
                            media={'references': {'enabled': True}}) as (adapter, root, remote, server):
        value = PNG + b'x' * 2200
        downloaded = owned(adapter.media, value)
        adapter.media.download = AsyncMock(return_value=downloaded)
        assert (await adapter.send_image('private:200', 'https://gchat.qpic.cn/image')).success
        assert (await adapter.send_image('private:200', 'base64://' + base64.b64encode(value).decode())).success
        from hermes_napcat.protocol import Incoming
        incoming = Incoming.parse(raw_event(message=[{'type': 'image', 'data': {'file': 'image-id'}}]))
        ref = adapter.media_refs.remember(incoming)[0]
        adapter.media_refs.set_downloaded(ref.media_id, incoming.target, downloaded)
        bind_session(monkeypatch, adapter, target='private:200')
        assert json.loads(await tools.qq_send_media({'media_type': 'image',
                                                    'source': 'media:' + ref.media_id}))['success']
        assert remote.complete == [value, value, value]


async def test_stream_disabled_or_size_limit_refuses_before_network(hermes_doubles, settings, tmp_path):
    root = tmp_path / 'output'
    root.mkdir()
    path = root / 'test.png'
    path.write_bytes(PNG + b'x' * 2048)
    for options in ({'mode': 'disabled'}, {'max_bytes': 1024}):
        adapter = make_adapter(hermes_doubles, settings, media={
            'inline_max_bytes': 1024, 'outbound_roots': [root], 'streaming': options})
        adapter.media = MediaStore(adapter.settings.media, tmp_path / 'cache')
        result = await adapter.send_image_file('private:200', str(path))
        assert not result.success
        adapter.transport.call.assert_not_called()


async def test_byte_budget_holds_busy_events_and_recall_has_reserved_lane(settings, raw_event):
    gate, entered = asyncio.Event(), asyncio.Event()
    received = []
    async def handler(event):
        if event.get('post_type') == 'notice':
            received.append(event)
            return
        entered.set()
        await gate.wait()
    async with fake_napcat() as server:
        transport = OneBotTransport(settings(ws_url=server['url'], event_queue_max_bytes=1024,
            event_workers=1, media={'references': {'enabled': True}}), handler)
        try:
            await transport.start()
            await server['ws'].send_json(raw_event('x' * 500))
            await entered.wait()
            await server['ws'].send_json(raw_event('x' * 500, message_id=2))
            await wait_until(lambda: transport.stats.dropped == 1)
            assert 0 < transport._queued_event_bytes <= 1024
            await server['ws'].send_json({'post_type': 'notice', 'notice_type': 'friend_recall',
                'self_id': 100, 'user_id': 200, 'message_id': -10, 'ignored_large_data': 'x' * 4000})
            await wait_until(lambda: len(received) == 1)
            assert 'ignored_large_data' not in received[0]
            assert await transport.call('get_status') == {}
        finally:
            gate.set()
            await transport.stop()
        assert transport._queued_event_bytes == 0


async def test_epoch_pin_survives_waiting_for_transport_send_lock(settings):
    async with fake_napcat() as server:
        transport = OneBotTransport(settings(ws_url=server['url']), ignore)
        try:
            await transport.start()
            await transport._send_lock.acquire()
            epoch = transport.connection_epoch
            task = asyncio.create_task(transport.call('get_status', expected_epoch=epoch))
            await asyncio.sleep(0)
            transport._epoch += 1  # Simulate replacement after the initial check, before write.
            transport._send_lock.release()
            with pytest.raises(NotConnected):
                await task
            assert len(server['requests']) == 1
        finally:
            await transport.stop()


async def test_data_uri_rich_and_forward_tools(monkeypatch, hermes_doubles, settings, tmp_path):
    adapter = make_adapter(hermes_doubles, settings, qq_tools={'enabled': True})
    adapter.media = MediaStore(adapter.settings.media, tmp_path / 'cache')
    bind_session(monkeypatch, adapter)
    value = PNG + b'x' * 10000
    source = 'data:image/png;base64,' + base64.b64encode(value).decode()
    assert json.loads(await tools.qq_send_message({'segments': [
        {'type': 'text', 'text': 'before'}, {'type': 'image', 'source': source},
        {'type': 'text', 'text': 'after'}]}))['success']
    assert json.loads(await tools.qq_send_forward({'nodes': [{'segments': [
        {'type': 'image', 'source': source}]}]}))['success']
    assert adapter.transport.call.await_count == 2


async def test_chunk_upload_works_on_reverse_ws(settings, tmp_path, unused_tcp_port):
    import aiohttp
    from test_transport import TOKEN
    config = settings(mode='reverse', listen_port=unused_tcp_port,
                      media={'streaming': {'chunk_bytes': 1024}})
    transport = OneBotTransport(config, ignore)
    remote = UploadServer()
    path = tmp_path / 'image.png'
    path.write_bytes(PNG + b'x' * 3000)
    await transport.start()
    task = None
    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(
                f'http://127.0.0.1:{unused_tcp_port}/onebot/v11',
                headers={'Authorization': 'Bearer ' + TOKEN, 'X-Self-ID': '100'},
            ) as ws:
                hello = await ws.receive_json()
                await ws.send_json({'status': 'ok', 'retcode': 0, 'echo': hello['echo'],
                                    'data': {'user_id': 100}})
                await asyncio.wait_for(transport.ready.wait(), 1)
                async def serve():
                    async for frame in ws:
                        if frame.type == aiohttp.WSMsgType.TEXT:
                            await remote(ws, json.loads(frame.data))
                task = asyncio.create_task(serve())
                uploader = StreamUploader(transport, config)
                ref = await uploader.upload(path, path.stat().st_size)
                ref.validate(transport)
                assert remote.complete == [path.read_bytes()]
                await uploader.close()
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await transport.stop()
