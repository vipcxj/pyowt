import asyncio
import base64
import json
import logging
import time
from typing import Any, Literal, overload

import socketio

from .common import ConferenceError, EventDispatcher, MessageEvent, OwtEvent
from .portal_types import (BWEResult, CandidateMessage, LoginInfo, LoginResult,
                           OfferAnswer, PublicationOrSubscriptionResult,
                           PublicationRequest, ReconnectionTicket,
                           ReloginResponse, RemovedCandidatesMessage,
                           SOACMessage, StreamControlInfo,
                           SubscriptionControlInfo, SubscriptionRequest,
                           TextSendMessage, UnpublicationRequest,
                           UnsubscriptionRequest)
from .utils import AsyncExceptionCollector, NullAsyncContext
from .log import configLogger

logger = configLogger(logger=logging.getLogger("owt-signaling"))

class SioSignaling(EventDispatcher):
    
    io: socketio.AsyncClient | None
    _lock: asyncio.Lock
    _loop: asyncio.AbstractEventLoop | None
    _ready_future: asyncio.Future[None]
    _loggedIn: bool = False
    _reconnectTimes: int = 0
    _reconnectionTicket: str | None = None
    _refreshReconnectionTicket: asyncio.Future | None = None
    reconnectionAttempts: int
    exception_collector: AsyncExceptionCollector | None = None
    
    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        super().__init__()
        self.io = None
        self._loop = loop
        self._lock = asyncio.Lock()
        self._ready_future = asyncio.Future(loop=loop)
        
    async def ready(self):
        async with self._lock:
            await self._ready_future
            
    async def markReady(self):
        if self.io is None:
            return
        async with self._lock:
            if not self._ready_future.done():
                self._ready_future.set_result(None)
                self._loggedIn = True
            
    async def markNotReady(self):
        if self.io is None:
            return
        async with self._lock:
            if self.io and self._ready_future.done():
                self._ready_future = asyncio.Future()
                self._loggedIn = False
                
    async def cancelReady(self, e: BaseException | None = None):
        async with self._lock:
            if not self._ready_future.done():
                if e and not isinstance(e, asyncio.CancelledError):
                    self._ready_future.set_exception(e)
                else:
                    self._ready_future.cancel()
            else:
                self._ready_future.cancel()
                    
    
        
    async def connect(self, host: str, login_info: LoginInfo) -> LoginResult:
        if self.io is not None:
            raise RuntimeError('Portal has been connected.')
        io = self.io = socketio.AsyncClient(ssl_verify=False)
        collector = self.exception_collector = AsyncExceptionCollector(loop=self._loop)
        if not login_info.get('reconnection'):
            login_info['reconnection'] = {
                'keepTime': 300
            }
        async def on_exception(e: BaseException):
            await self.disconnect(True, True, e)
            
        await collector.add_listener(on_exception)
        
        await io.connect(host)
        await self.dispatchEventAwaitable(OwtEvent('connected'))
        def create_message_handler(notification: str):
            async def message_handler(data: Any):
                async with collector.collect():
                    await self.dispatchEventAwaitable(MessageEvent(
                        'data',
                        message={
                            'notification': notification,
                            'data': data,
                        }
                    ))
            return message_handler
    
        for notification in ['participant', 'text', 'stream', 'progress']:
            io.on(notification, create_message_handler(notification))
            
        async def on_drop(data: Any):
            async with collector.collect():
                await self.disconnect(False, True)
        io.on('drop', on_drop)
        
        async def on_disconnect():
            async with collector.collect():
                logger.debug('signaling disconnected')
                await self.markNotReady()
                # self._clearReconnectionTask()
        io.on('disconnect', on_disconnect)
            
        login_result = await self.send('login', login_info)
        await self.markReady()
        reconnectionTicket = login_result.get('reconnectionTicket')
        assert reconnectionTicket is not None
        self._onReconnectionTicket(reconnectionTicket)
        
        async def on_connect():
            async with collector.collect():
                logger.debug('signaling reconnected')
                await self.dispatchEventAwaitable(OwtEvent('reconnected'))
                try:
                    assert self._reconnectionTicket is not None
                    relogin_resp = await self.send('relogin', self._reconnectionTicket)
                    await self.markReady()
                    self._onReconnectionTicket(relogin_resp['ticket'])
                except BaseException as e:
                    logger.error(f'Something error happened when relogining. {e}')
                    raise e

        io.on('connect', on_connect)
        return login_result

    @overload
    async def send(self, requestName: Literal['soac'], requestData: SOACMessage) -> None: ...
    @overload
    async def send(self, requestName: Literal['publish'], requestData: PublicationRequest) -> PublicationOrSubscriptionResult: ...
    @overload
    async def send(self, requestName: Literal['unpublish'], requestData: UnpublicationRequest) -> None: ...
    @overload
    async def send(self, requestName: Literal['stream-control'], requestData: StreamControlInfo) -> None: ...
    @overload
    async def send(self, requestName: Literal['subscribe'], requestData: SubscriptionRequest) -> PublicationOrSubscriptionResult: ...
    @overload
    async def send(self, requestName: Literal['unsubscribe'], requestData: UnsubscriptionRequest) -> None: ...
    @overload
    async def send(self, requestName: Literal['subscription-control'], requestData: SubscriptionControlInfo) -> BWEResult | None: ...
    @overload
    async def send(self, requestName: Literal['login'], requestData: LoginInfo) -> LoginResult: ...
    @overload
    async def send(self, requestName: Literal['relogin'], requestData: str) -> ReloginResponse: ...
    @overload
    async def send(self, requestName: Literal['refreshReconnectionTicket']) -> str: ...
    @overload
    async def send(self, requestName: Literal['text'], requestData: TextSendMessage) -> None: ...
    async def send(self, requestName: str, requestData: Any = None):
        if self.io is None:
            raise ConferenceError('Portal is not connected.')
        future = asyncio.Future(loop=self._loop)
        def resp(status: str, data: Any = None):
            if status == 'ok' or status == 'success':
                future.set_result(data)
            else:
                future.set_exception(ConferenceError(data))
        await self.io.emit(requestName, requestData, callback=resp)
        return await future
    
    async def send_text(self, message: str, to: str = 'all'):
        await self.ready()
        return await self.send('text', { 'message': message, 'to': to })
    
    async def publish(self, request: PublicationRequest) -> PublicationOrSubscriptionResult:
        await self.ready()
        return await self.send('publish', request)
    
    async def unpublish(self, pub_id: str, maybe_not_exist: bool = True) -> None:
        await self.ready()
        try:
            return await self.send('unpublish', { 'id': pub_id })
        except ConferenceError as e:
            if e.message and e.message.lower() != 'Stream does NOT exist'.lower() or not maybe_not_exist:
                raise e
        
    async def subscribe(self, request: SubscriptionRequest) -> PublicationOrSubscriptionResult:
        await self.ready()
        return await self.send('subscribe', request)
    
    async def unsubscribe(self, sub_id: str, maybe_not_exist: bool = True):
        await self.ready()
        try:
            await self.send('unsubscribe', { 'id': sub_id })
        except ConferenceError as e:
            if e.message and e.message.lower() != 'Subscription does NOT exist'.lower() or not maybe_not_exist:
                raise e
            
    async def soac(self, transport_id: str, signaling: OfferAnswer | CandidateMessage | RemovedCandidatesMessage) -> None:
        await self.ready()
        await self.send('soac', { 'id': transport_id, 'signaling': signaling })    
    
    async def disconnect(self, logout: bool = True, permit_duplicate_disconnect = False, e: BaseException | None = None):
        if self.io is None:
            if not permit_duplicate_disconnect:
                raise asyncio.InvalidStateError('Portal is not connected.')
            else:
                return
        async with self.exception_collector.collect() if self.exception_collector else NullAsyncContext():
            await self.cancelReady(e)
            io = self.io
            self.io = None
            if io.connected:
                if logout:
                    try:
                        await io.emit('logout')
                    except BaseException as e:
                        logger.warning(f'Portal logout failed: {e}')
                self._clearReconnectionTask()
                try:
                    await io.disconnect()
                except BaseException as e:
                    logger.warning(f'IO socket disconnect failed: {e}')
            await self.dispatchEventAwaitable(OwtEvent('disconnect'))
            if self.exception_collector:
                self.exception_collector.complete()
                self.exception_collector = None

        
    def _onReconnectionTicket(self, ticketString: str):
        self._reconnectionTicket = ticketString
        ticket: ReconnectionTicket = json.loads(base64.b64decode(ticketString))
        
        # Refresh ticket 1 min or 10 seconds before it expires.
        now = time.time() * 1000
        millisecondsInOneMinute = 60 * 1000
        millisecondsInTenSeconds = 10 * 1000
        if ticket['notAfter'] <= now - millisecondsInTenSeconds:
            logger.warning('Reconnection ticket expires too soon.')
            return
        
        refreshAfter = ticket['notAfter'] - now - millisecondsInOneMinute
        if refreshAfter < 0:
            refreshAfter = ticket['notAfter'] - now - millisecondsInTenSeconds
        
        self._clearReconnectionTask()
        
        async def refreshReconnectionTicket():
            await asyncio.sleep(refreshAfter / 1000)
            io = self.io
            if io is not None:
                new_reconnect_ticket = await self.send('refreshReconnectionTicket')
                self._onReconnectionTicket(new_reconnect_ticket)
        self._refreshReconnectionTicket = asyncio.ensure_future(refreshReconnectionTicket(), loop=self._loop)
    
    def _clearReconnectionTask(self):
        if self._refreshReconnectionTicket is not None:
            self._refreshReconnectionTicket.cancel()
        self._refreshReconnectionTicket = None
