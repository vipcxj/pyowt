import asyncio
from aioquic.asyncio import QuicConnectionProtocol
from aioquic.asyncio.client import connect, QuicConfiguration
from aioquic.h3.connection import H3Connection

import json
import base64
import logging
import struct
from urllib.parse import urlparse
from contextlib import AsyncExitStack
from typing import TypedDict

from .signaling import SioSignaling
from .common import EventDispatcher
from .stream import LocalStream, StreamSourceInfo, RemoteStream, OwtEvent
from .quic_types import QuicBidirectionalStream
from .common import ConferenceError
from .publication import Publication, TransportSettings
from .subscription import Subscription
from .log import configLogger

logger = configLogger(logger=logging.getLogger('owt-quic'))

class QuicToken(TypedDict):
    transportId: str

class QuicChannel(EventDispatcher):
    url: str
    tokenString: str
    signaling: SioSignaling
    configure: QuicConfiguration | None
    _transportId: str
    _loop: asyncio.AbstractEventLoop | None
    _ready: asyncio.Event
    _ready_lock: asyncio.Lock
    _lock: asyncio.Lock
    _quic: QuicConnectionProtocol | None
    _stack: AsyncExitStack | None
    _publications: dict[str, Publication]
    _subscriptions: dict[str, Subscription]
    _sub_promises: dict[str, asyncio.Future[Subscription]]
    _sub_streams: dict[str, QuicBidirectionalStream]
    
    def __init__(self, url: str, tokenString: str, signaling: SioSignaling, configure: QuicConfiguration | None = None, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.signaling = signaling
        self.url = url
        self.tokenString = tokenString
        self.configure = configure
        token: QuicToken = json.loads(base64.b64decode(tokenString))
        self._transportId = token['transportId']
        self._loop = loop
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._ready_lock = asyncio.Lock()
        self._publications = {}
        self._subscriptions = {}
        self._sub_streams = {}
        self._sub_promises = {}
        self._quic = None
        self._stack = None
        
    async def send_data(self, writer: asyncio.StreamWriter, data: str | bytes, drain: bool = True):
        if isinstance(data, str):
            content_bytes = data.encode(encoding='utf-8')
        else:
            content_bytes = data
        length = len(content_bytes)
        length_bytes = struct.pack('<I', length)
        writer.write(length_bytes)
        writer.write(content_bytes)
        if drain:
            await writer.drain()
    
    ZERO_BYTES: bytes = b'\0' * 16
    
    async def do_nothing(self, _):
        pass
    
    def _create_subscription(self, sub_id: str, stream: QuicBidirectionalStream) -> Subscription:
        async def stop():
            await self._unsubscribe(sub_id)
        return Subscription(
            id=sub_id,
            track=[],
            quic_stream=stream,
            transport=TransportSettings(type='quic', id=self._transportId, rtpTransceivers=[]),
            stop=stop,
            mute=self.do_nothing,
            unmute=self.do_nothing,
        )
            
    
    async def bind_subscription(self, stream: QuicBidirectionalStream):
        async with stream.lock:
            try:
                bytes = await stream.reader.readexactly(16)
            except asyncio.IncompleteReadError as e:
                logger.error(f'Stream closed unexpectedly.')
                return
        sub_id = bytes.hex()
        async with self._lock:
            assert sub_id not in self._subscriptions, f'Subscription with id {sub_id} exists.'
            assert sub_id not in self._sub_streams, f'Subscription stream with id {sub_id} exists.'
            self._sub_streams[sub_id] = stream
            future = self._sub_promises.get(sub_id)
        if future is not None and not future.done():
            del self._sub_promises[sub_id]
            subscription = self._create_subscription(sub_id, stream)
            self._subscriptions[sub_id] = subscription
            future.set_result(subscription)
        
    
    def on_stream_add(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        quic_stream = QuicBidirectionalStream(reader=reader, writer=writer, lock=asyncio.Lock())
        asyncio.ensure_future(self.bind_subscription(quic_stream), loop=self._loop)
    
    async def init(self):
        async with self._ready_lock:
            if self._ready.is_set():
                raise asyncio.InvalidStateError('quic connection exists!')
            parsed_result = urlparse(self.url)
            assert parsed_result.scheme in (
                "https",
                "wss",
            ), f"Only https:// or wss:// URLs are supported, but got {self.url}"
            host = parsed_result.hostname
            port = parsed_result.port or 7700
            assert host, f"no host: {self.url}"
            if self._quic:
                logger.warning('quic connection exists!')
                assert self._stack
                await self._stack.aclose()
                self._stack = None
                self._quic = None
            assert self._stack is None
            self._stack = AsyncExitStack()
            try:
                self._quic = await self._stack.enter_async_context(connect(host, port, configuration=self.configure, stream_handler=self.on_stream_add))
                reader, writer = await self._quic.create_stream()
                # 128 bit of zero indicates this is a stream for signaling.
                writer.write(self.ZERO_BYTES)
                await writer.drain()
                await self.send_data(writer, self.tokenString, False)
                # when signaling, server return 128 bit of zero again
                res = await reader.read(16)
                assert res == self.ZERO_BYTES
                self._ready.set()
                logger.debug('Authentication success.')
            except BaseException as e:
                if self._quic and self._quic._connected:
                    try:
                        self._quic.close()
                        await asyncio.wait_for(self._quic.wait_closed(), timeout=10)
                    except asyncio.TimeoutError:
                        logger.warning('Failed quic connection closed timeout.')
                        pass
                    except BaseException as e0:
                        logger.exception(e0)
                        pass
                raise e
        
    async def close(self, may_be_closed: bool = True):
        async with self._ready_lock:
            if not self._ready.is_set():
                if not may_be_closed:
                    raise asyncio.InvalidStateError('The quic channel has been closed.')
                else:
                    return
            publications = list(self._publications.values())
            for pub in publications:
                try:
                    await pub.stop()
                except BaseException as e:
                    logger.exception(e)
            publications.clear()
            subscriptions = list(self._subscriptions.values())
            for sub in subscriptions:
                try:
                    await sub.stop()
                except BaseException as e:
                    logger.exception(e)
            subscriptions.clear()
            for f in self._sub_promises.values():
                f.cancel()
            self._sub_promises.clear()
            self._sub_streams.clear()
            assert self._stack
            await self._stack.aclose()
            self._stack = None
            self._quic = None
            self._ready.clear()
            
    def is_ready(self):
        return self._ready.is_set()
    
    async def wait_ready(self):
        return await self._ready.wait()
            
    
    async def createSendStream(self, attributes: dict | None = None, is_unidirectional: bool = False) -> LocalStream:
        quic = self._quic
        if not quic:
            raise asyncio.InvalidStateError('The quic connection has not been inited or has been closed.')
        reader, writer = await quic.create_stream(is_unidirectional=is_unidirectional)
        quic_stream = QuicBidirectionalStream(reader=reader, writer=writer, lock=asyncio.Lock())
        return LocalStream(tracks=[], quic_stream=quic_stream, source=StreamSourceInfo(data=True), attributes=attributes or {})
    
    async def _unpublish(self, pub_id: str):
        self._publications.pop(pub_id, None)
        try:
            await self.signaling.unpublish(pub_id)
        except BaseException as e:
            logger.warning(f'MCU returns negative ack for unpublishing, {e}')
    
    async def publish(self, stream: LocalStream):
        if not stream.source.data:
            raise ConferenceError('Only data stream can be published by quic channel.')
        assert stream.quic_stream, 'The quic channel require the local stream has quic stream.'
        pub = await self.signaling.publish({
            'data': True,
            'transport': {
                'type': 'quic',
                'id': self._transportId,
            }
        })
        pub_id = pub['id']
        try:
            assert pub_id not in self._publications, f'The publication with id: {id} has exist.'
            if pub['transportId'] != self._transportId:
                raise ConferenceError(f'Transport ID not match.')
            writer = stream.quic_stream.writer
            id_bytes = bytes.fromhex(pub_id)
            assert len(id_bytes) == 16, f'Invalid publish id, it should be 128 bit hex string, but now is: {pub_id}'
            async with stream.quic_stream.lock:
                await self.send_data(writer, id_bytes)
            async def do_nothing(_):
                pass
            async def stop():
                self._publications.pop(pub_id, None)
                try:
                    await self.signaling.unpublish(pub_id)
                except BaseException as e:
                    logger.warning(f'MCU returns negative ack for unpublishing, {e}')
            publication = Publication(
                pub_id,
                transport=TransportSettings('quic', self._transportId, []),
                stop=stop,
                mute=do_nothing,
                unmute=do_nothing,
            )
            self._publications[pub_id] = publication
            return publication
        except BaseException:
            try:
                await self._unpublish(pub_id)
            except BaseException as e0:
                logger.warning(f'Unpublish failed. {e0}')
            raise
        
    async def _unsubscribe(self, sub_id: str):
        f = self._sub_promises.pop(sub_id, None)
        if f is not None:
            f.cancel()
        self._subscriptions.pop(sub_id, None)
        try:
            await self.signaling.unsubscribe(sub_id)
        except BaseException:
            logger.warning('MCU returns negative ack for unsubscribing, {e}')
        
    async def subscribe(self, stream: RemoteStream):
        if not stream.source.data:
            raise ConferenceError('Only data stream can be subscribed by quic channel.')
        assert stream.quic_stream, 'The quic channel require the remote stream has quic stream.'
        sub = await self.signaling.subscribe({
            'data': { 'from': stream.id },
            'transport': { 'type': 'quic', 'id': self._transportId },
        })
        
        sub_id = sub['id']
        try:
            assert sub_id not in self._subscriptions, f'The subscription with id: {id} has exist.'
            if sub['transportId'] != self._transportId:
                raise ConferenceError(f'Transport ID not match.')
            async with self._lock:
                quic_stream = self._sub_streams.get(sub_id, None)
                if quic_stream is not None:
                    subscription = self._create_subscription(sub_id, quic_stream)
                    self._subscriptions[sub_id] = subscription
                    return subscription
                else:
                    assert sub_id not in self._sub_promises
                    future: asyncio.Future[Subscription] = asyncio.Future(loop=self._loop)
                    self._sub_promises[sub_id] = future
            return await future
        except BaseException:
            try:
                await self._unsubscribe(sub_id)
            except BaseException as e0:
                logger.warning(f'unsubscribe failed. {e0}')
            raise