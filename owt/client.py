import asyncio
from aioquic.asyncio.client import QuicConfiguration
import base64
import json
import logging
import sys
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Callable, cast, overload

from aiortc import RTCConfiguration, RTCIceServer, RTCRtpTransceiver

if sys.version_info < (3, 11):
    from typing_extensions import Any, Literal, NotRequired, TypedDict
else:
    from typing import Any, Literal, NotRequired, TypedDict
    
if __name__ == '__main__':
    from pathlib import Path
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[1]
    sys.path.append(str(top))
    try:
        sys.path.remove(str(parent))
    except ValueError: # already removed
        pass
    __package__ = '.'.join(parent.parts[len(top.parts):])

from .channel import OwtChannel
from .quic import QuicChannel
from .quic_types import QuicConfigurationDict, toQuicConfiguration
from .common import ConferenceError, EventDispatcher, MessageEvent, OwtEvent
from .constants import AudioSourceInfo, VideoSourceInfo
from .info import ConferenceInfo
from .participant import Participant
from .portal_types import (LoginInfo, ParticipantAction,
                           StreamAddUpdateMessage, StreamInfo,
                           StreamRemoveUpdateMessage, StreamUpdateMessage,
                           StreamUpdateUpdateMessage, TextReceiveMessage)
from .publication import PublishOptions
from .signaling import SioSignaling
from .stream import (ActiveAudioInputChangeEvent, LayoutChangeEvent,
                     LocalStream, RemoteMixedStream, RemoteStream, StreamEvent,
                     StreamSourceInfo, convertToPublicationSettings,
                     convertToSubscriptionCapabilities)
from .subscription import SubscribeOptions
from .utils import (AsyncExceptionCollector, NullAsyncContext, ensure_future)
from .log import configLogger

logger = configLogger(logger=logging.getLogger('owt-client'))

class SignalingState(Enum):
    READY = 1
    CONNECTING = 2
    CONNECTED = 3
    


class Token(TypedDict):
    host: str
    secure: NotRequired[bool | None]
    webTransportUrl: NotRequired[str | None]
    

@dataclass
class WebTransportHash:
    algorithm: str
    value: bytes
    
@dataclass
class WebTransportOptions:
    allowPooling: bool = False
    requireUnreliable: bool = False
    serverCertificateHashes: list[WebTransportHash] = field(default_factory=list)
    congestionControl: Literal['default', 'hroughput', 'low-latency'] = 'default'
    
class ParticipantEvent(OwtEvent):
    participant: Participant
    
    def __init__(self, type: str, participant: Participant) -> None:
        super().__init__(type)
        self.participant = participant
        
class IceServerDict(TypedDict):
    username: NotRequired[str | None]
    credential: NotRequired[str | None]
    credentialType: NotRequired[str | None]
    urls: str | list[str]
        
class RTCConfigurationDict(TypedDict):
    iceServers: NotRequired[list[IceServerDict | RTCIceServer] | None]
    
def toRTCConfiguration(value: RTCConfiguration | RTCConfigurationDict) -> RTCConfiguration:
    if isinstance(value, RTCConfiguration):
        return value
    iceservers = value.get('iceServers')
    if iceservers is None:
        return RTCConfiguration()
    else: 
        return RTCConfiguration(
            iceServers=[RTCIceServer(
                urls=cast(Any, ice_server['urls']),
                username=ice_server.get('username'),
                credential=ice_server.get('credential'),
                credentialType=ice_server.get('credentialType', 'password') or 'password',
            ) if isinstance(ice_server, dict) else ice_server for ice_server in iceservers]
        )
    
class ConferenceClientConfigurationDict(TypedDict):
    rtcConfiguration: NotRequired[RTCConfiguration | RTCConfigurationDict | None]
    webTransportConfiguration: NotRequired[QuicConfiguration | QuicConfigurationDict | None]

@dataclass
class ConferenceClientConfiguration:
    rtcConfiguration: RTCConfiguration | RTCConfigurationDict | None = None
    webTransportConfiguration: QuicConfiguration | QuicConfigurationDict | None = None
    
    @property
    def rtcConfigurationObject(self) -> RTCConfiguration:
        if self.rtcConfiguration is None:
            return RTCConfiguration()
        else:
            return toRTCConfiguration(self.rtcConfiguration)
        
    @property
    def webTransportConfigurationObject(self) -> QuicConfiguration | None:
        if isinstance(self.webTransportConfiguration, dict):
            return toQuicConfiguration(self.webTransportConfiguration)
        return self.webTransportConfiguration
    
    @staticmethod
    def fromConfigLike(value: "ConferenceClientConfiguration | ConferenceClientConfigurationDict | None"):
        if value is None:
            return ConferenceClientConfiguration()
        if isinstance(value, ConferenceClientConfiguration):
            return value
        rtcConfiguration = value.get('rtcConfiguration')
        if rtcConfiguration:
            rtcConfiguration = toRTCConfiguration(rtcConfiguration)
        webTransportConfiguration = value.get('webTransportConfiguration')
        if webTransportConfiguration:
            webTransportConfiguration = toQuicConfiguration(webTransportConfiguration)
        return ConferenceClientConfiguration(rtcConfiguration=rtcConfiguration, webTransportConfiguration=webTransportConfiguration)

class ConferenceClient(EventDispatcher):
    config: ConferenceClientConfiguration
    signalingState: SignalingState = SignalingState.READY
    signaling: SioSignaling
    me: Participant | None
    __ready: asyncio.Event
    __ready_lock: asyncio.Lock
    __loop: asyncio.AbstractEventLoop | None
    __remoteStreams: dict[str, RemoteStream]
    __participants: dict[str, Participant]
    __channels: dict[str, OwtChannel]
    quicTransportChannel: QuicChannel | None
    exception_collector: AsyncExceptionCollector | None
    
    def __init__(self, config: ConferenceClientConfiguration | ConferenceClientConfigurationDict | None = None, loop: asyncio.AbstractEventLoop | None = None) -> None:
        super().__init__()
        self.config = ConferenceClientConfiguration.fromConfigLike(config)
        self.me = None
        self.__loop = loop
        self.__ready = asyncio.Event()
        self.__ready_lock = asyncio.Lock()
        self.signaling = SioSignaling(loop=loop)
        self.__remoteStreams = {}
        self.__participants = {}
        self.__channels = {}
        self.quicTransportChannel = None
        self.exception_collector = None
        self.signaling.addEventListener('data', self.onDataMessage)
        self.signaling.addEventListener('disconnect', self.onDisconnect)        
        
    @property
    def loop(self):
        return self.__loop
    
    @property
    def remoteStreams(self):
        return self.__remoteStreams
    
    @property
    def participants(self):
        return self.__participants
    
    @property
    def channels(self):
        return self.__channels
    
    async def onDisconnect(self, _):
        await self.leave()
        await self.dispatchEventAwaitable(OwtEvent('serverdisconnected'))
    
    async def onDataMessage(self, evt: MessageEvent):
        await self.onSignalingMessage(notification=evt.message['notification'], data=evt.message['data'])
    
    async def onSignalingMessage(self, notification: str, data: Any):
        if notification == 'soac' or notification == 'progress':
            id = data.get('id')
            if id in self.channels:
                await self.channels[id].onMessage(notification, data)
            elif False: # TODO: quicTransportChannel part
                pass
            else:
                logger.warning(f'Cannot find a channel for incoming data: {id}.')
        elif notification == 'stream':
            stream_data = cast(StreamUpdateMessage, data)
            status = stream_data['status']
            if status == 'add':
                stream_data = cast(StreamAddUpdateMessage, stream_data)
                await self.fireStreamAdded(stream_data['data'])
            elif status == 'remove':
                stream_data = cast(StreamRemoveUpdateMessage, stream_data)
                await self.fireStreamRemoved(stream_data)
            elif status == 'update':
                stream_data = cast(StreamUpdateUpdateMessage, stream_data)
                if stream_data['data']['field'] == 'audio.status' or stream_data['data']['field'] == 'video.status':
                    for channel in self.channels.values():
                        await channel.onMessage(notification, stream_data)
                elif stream_data['data']['field'] == 'activeInput':
                    await self.fireActiveAudioInputChange(stream_data)
                elif stream_data['data']['field'] == 'video.layout':
                    await self.fireLayoutChange(stream_data)
                elif stream_data['data']['field'] == '.':
                    await self.updateRemoteStream(stream_data['data']['value'])
                else:
                    raise ConferenceError('not support yet.')
            else:
                raise ConferenceError('not support yet.')
        elif notification == 'text':
            await self.fireMessageReceived(data)
        elif notification == 'participant':
            await self.fireParticipantEvent(data)
        else:
            logger.warning(f'Unknown notification: {notification}')
            
    async def updateRemoteStream(self, stream_info: StreamInfo):
        if stream_info['id'] not in self.remoteStreams:
            logger.warning(f'Cannot find specific remote stream: {stream_info["id"]}')
            return
        stream = self.remoteStreams[stream_info['id']]
        stream.settings = convertToPublicationSettings(stream_info.get('media'))
        stream.extraCapabilities = convertToSubscriptionCapabilities(stream_info.get('media'))
        await stream.dispatchEventAwaitable(OwtEvent('updated'))
    
    def _createRemoteStream(self, stream_info: StreamInfo) -> RemoteStream:
        if stream_info['type'] == 'mixed':
            return RemoteMixedStream(stream_info)
        media = stream_info.get('media')
        audio_track = next((t for t in (media['tracks'] if media else []) if t['type'] == 'audio'), None)
        video_track = next((t for t in (media['tracks'] if media else []) if t['type'] == 'video'), None)
        info = stream_info.get('info')
        owner = info.get('owner')
        stream = RemoteStream(
            tracks=[],
            quic_stream=None,
            id=stream_info.get('id'),
            origin=owner,
            source=StreamSourceInfo(
                audio=cast(AudioSourceInfo | None, audio_track.get('source') if audio_track is not None else None),
                video=cast(VideoSourceInfo | None, video_track.get('source') if video_track is not None else None),
                data=stream_info.get('data', False),
            ),
            attributes=info.get('attributes') or {},
        )
        stream.settings = convertToPublicationSettings(media)
        stream.extraCapabilities = convertToSubscriptionCapabilities(media)
        return stream
    
    def createPeerConnectionChannel(self) -> OwtChannel:
        channel = OwtChannel(signaling=self.signaling, rtc_configure=self.config.rtcConfigurationObject, loop=self.__loop)
        def on_id_msg(evt: MessageEvent):
            if evt.message not in self.channels:
                self.channels[evt.message] = channel
            else:
                logger.warning(f'Channel already exists {evt.message}')
        def on_ended(_):
            id = next((k for k, c in self.channels.items() if c == channel), None)
            if id is None:
                logger.warning('Receive ended message from unknown channel.')
            else:
                del self.channels[id]
        channel.addEventListener('id', on_id_msg)
        channel.addEventListener('ended', on_ended)
        return channel
    
    def collector_context(self):
        collector = self.exception_collector
        return collector.collect() if collector else NullAsyncContext()
    
    async def join(self, tokenString: str):
        async with self.__ready_lock:
            if self.__ready.is_set():
                raise ConferenceError('The client has joined the conference, to join another conference, leave at first.')
            token: Token = json.loads(base64.b64decode(tokenString))
            isSecured = token.get('secure', False)
            host = token['host']
            if host.find('http') == -1:
                host = f'https://{host}' if isSecured else f'http://{host}'
            if self.signalingState != SignalingState.READY:
                raise ConferenceError('connection state invalid')
            self.signalingState = SignalingState.CONNECTING
            self.exception_collector = AsyncExceptionCollector(loop=self.__loop)
            try:
                login_info: LoginInfo = {
                    'token': tokenString,
                    'userAgent': {
                        'sdk': {
                            'version': '5.0',
                            'type': 'JavaScript',
                        }
                    },
                    'protocol': '1.2'
                }
                login_result = await self.signaling.connect(host, login_info)
                self.signalingState = SignalingState.CONNECTED
                room = login_result['room']
                streams = room.get('streams')
                if streams:
                    for st in streams:
                        self.remoteStreams[st['id']] = self._createRemoteStream(st)
                participants = room.get('participants')
                if participants:
                    for p in participants:
                        self.participants[p['id']] = Participant(p['id'], p['role'], p['user'])
                        if p['id'] == login_result['id']:
                            self.me = self.participants[p['id']]
                quic_url = token.get('webTransportUrl')
                if quic_url:
                    quic_token = login_result.get('webTransportToken')
                    if not quic_token:
                        logger.warning('Unable to find quic token in login result, but the login token support it.')
                    else:
                        self.quicTransportChannel = QuicChannel(quic_url, quic_token, self.signaling, configure=self.config.webTransportConfigurationObject)
                        await self.quicTransportChannel.init()
                assert self.me is not None
                conferenceInfo = ConferenceInfo(
                    id=room['id'],
                    participants=list(self.participants.values()),
                    remoteStreams=list(self.remoteStreams.values()),
                    self=self.me,
                )
                self.__ready.set()
                return conferenceInfo
            except Exception as e:
                await self._try_leave()
                raise e
        
    async def _try_leave(self):
        if self.__ready.is_set():
            self.__ready.clear()
        channels = list(self.channels.values())
        for channel in channels:
            await channel.close()
        self.channels.clear()
        if self.quicTransportChannel:
            await self.quicTransportChannel.close()
            self.quicTransportChannel = None
        await self.signaling.disconnect(permit_duplicate_disconnect=True)
        self.clean()
        self.signalingState = SignalingState.READY
        self.exception_collector = None
        
    async def leave(self, may_be_leaved: bool = True):
        if not self.__ready.is_set():
            if may_be_leaved:
                return
            else:
                raise asyncio.InvalidStateError('The client has leaved the conference.')
        async with self.__ready_lock:
            if not self.__ready.is_set():
                if may_be_leaved:
                    return
                else:
                    raise asyncio.InvalidStateError('The client has leaved the conference.')
            await self._try_leave()
            
    def joined(self):
        return self.__ready.is_set()
    
    async def wait_joined(self):
        return await self.__ready.wait()
        
    async def publish(self, stream: LocalStream, options: PublishOptions | list[RTCRtpTransceiver] | None = None, video_codecs: list[str] | None = None):
        if not self.joined():
            raise asyncio.InvalidStateError('Join a conference at first.')
        if stream.source.data:
            if not self.quicTransportChannel:
                raise ConferenceError('The quic channel is not support, so unable to publish data stream.')
            return await self.quicTransportChannel.publish(stream)
        channel = self.createPeerConnectionChannel()
        pub = await channel.publish(stream, options, video_codecs)
        async def on_ended(_):
            await channel.close()
        pub.addEventListener('ended', on_ended)
        return pub
    
    async def subscribe(self, stream: RemoteStream, options: SubscribeOptions | None = None):
        if not self.joined():
            raise asyncio.InvalidStateError('Join a conference at first.')
        if stream.source.data:
            if stream.source.audio or stream.source.video:
                raise ValueError('Invalid source info. A remote stream is either a data stream or a media stream.')
            if not self.quicTransportChannel:
                raise ConferenceError('The quic channel is not support, so unable to subscribe data stream.')
            return await self.quicTransportChannel.subscribe(stream)
        else:
            channel = self.createPeerConnectionChannel()
            sub = await channel.subscribe(stream, options)
            async def on_ended(_):
                await channel.close()
            sub.addEventListener('ended', on_ended)
            return sub
        
    async def wait_for_stream(self, filter: Callable[[RemoteStream], bool], timeout: float = 0):
        stream_event = asyncio.Event()
        def on_stream_add(evt: StreamEvent):
            if filter(evt.stream):
                stream_event.set()
                self.removeEventListener('streamadded', on_stream_add)
        self.addEventListener('streamadded', on_stream_add)
        stream = next((st for st in self.remoteStreams.values() if filter(st)), None)
        if stream:
            self.removeEventListener('streamadded', on_stream_add)
            return stream
        else:
            if timeout > 0:
                await asyncio.wait_for(stream_event.wait(), timeout=timeout)
            else:
                await stream_event.wait()
            stream = next((st for st in self.remoteStreams.values() if filter(st)), None)
            assert stream
            return stream
        
    def consume_streams(self, callback: Callable[[RemoteStream], Any], auto_resume: bool | int = True, timeout: float = 0):
        """consume the streams

        Args:
            callback invoked on all existing streams first, then will invoked when the new stream reccived.
            auto_resume (bool | int, optional): if true, the retry the callback when it raise the exception except for the stream completed. 
            If provide with a number, the negative number means try any times, zero means not resume, and positive number means the try times.
            Defaults to True.
            timeout (float, optional): if the existing streams are all consumed (Here consumed means the callback completed) 
            and there is no new stream incoming for timeout second, the wait method will return. Defaults to 0. A zero or negative number means wait forever.

        Raises:
            asyncio.InvalidStateError: This means this method has some bug. it should never raised.

        Returns:
            Handle: a hande which can be to wait, close and renew (refresh the timeout).
        """
        loop = self.__loop
        class Tasks:
            tasks: list[asyncio.Future]
            lock: Lock
            state: Literal['running', 'timeouting', 'timeouted', 'error', 'closing', 'closed']
            timeout: float
            working_future: asyncio.Future
            timeout_future: asyncio.Future
            
            def __init__(self, timeout: float) -> None:
                self.tasks = []
                self.lock = Lock()
                self.working_future = asyncio.Future()
                self.working_future.set_result(None)
                self.state = 'timeouting'
                self.timeout_future = asyncio.ensure_future(self._do_timeout(), loop=loop)
                self.timeout = timeout
                    
                    
            async def _do_timeout(self):
                if self.timeout > 0:
                    await asyncio.sleep(self.timeout)
                    with self.lock:
                        if self.state == 'timeouting':
                            self.state = 'timeouted'
                        
                
            async def wait(self, close: bool = True) -> None:
                while True:
                    try:
                        await self.working_future
                        await self.timeout_future
                    except asyncio.CancelledError:
                        if self.state == 'closing' or self.state == 'closed':
                            break
                    except BaseException:
                        self.close()
                        raise
                    else:
                        with self.lock:
                            if self.state == 'timeouted':
                                self.state = 'closing'
                                break
                            elif self.state == 'closed' or self.state == 'closing':
                                break
                if close:
                    self.close()
                    
            def renew(self) -> bool:
                if self.state == 'closed' or self.state == 'closing' or self.state == 'error':
                    return False
                with self.lock:
                    if self.state == 'closed' or self.state == 'closing' or self.state == 'error':
                        return False
                    elif self.state == 'running':
                        return True
                    elif self.state == 'timeouting' or self.state == 'timeouted':
                        self.state = 'timeouting'
                        self.timeout_future = asyncio.ensure_future(self._do_timeout(), loop=loop)
                        return True
                    else:
                        raise asyncio.InvalidStateError(f'Invalid state: {self.state}')
                
            def _on_task_done(self, task: asyncio.Future):
                if self.state == 'closed' or self.state == 'closing' or self.state == 'error':
                    return
                if task in self.tasks:
                    with self.lock:
                        if self.state == 'closed' or self.state == 'closing' or self.state == 'error':
                            return
                        if task in self.tasks:
                            self.tasks.remove(task)
                            if not self.tasks:
                                assert not self.working_future.done()
                                self.working_future.set_result(None)
                                assert self.timeout_future.done()
                                self.state = 'timeouting'
                                self.timeout_future = asyncio.ensure_future(self._do_timeout(), loop=loop)
                
            def add_task(self, task: asyncio.Future):
                if self.state == 'closed' or self.state == 'closing' or self.state == 'error':
                    task.cancel()
                    return
                with self.lock:
                    if self.state == 'closed' or self.state == 'closing' or self.state == 'error':
                        task.cancel()
                    else:
                        if not self.tasks:
                            assert self.state != 'running'
                            assert self.working_future.done()
                            self.state = 'running'
                            self.working_future = asyncio.Future()
                            self.timeout_future.cancel()
                        task.add_done_callback(self._on_task_done)
                        self.tasks.append(task)
                        
            def set_exception(self, e: BaseException):
                if self.state == 'closed' or self.state == 'closing' or self.state == 'error':
                    return
                with self.lock:
                    if self.state == 'closed' or self.state == 'closing' or self.state == 'error':
                        return
                    self.state = 'error'
                    for task in self.tasks:
                        task.cancel()
                    self.working_future.cancel()
                    self.working_future = asyncio.Future()
                    self.working_future.set_exception(e)
                    self.timeout_future.cancel()
                    self.timeout_future = asyncio.Future()
                    self.timeout_future.set_exception(e)
                    
                        
            def close(self):
                if self.state == 'closed':
                    return
                with self.lock:
                    if self.state == 'closed':
                        return
                    else:
                        self.state = 'closed'
                        for task in self.tasks:
                            task.cancel()
                        self.tasks.clear()
                        self.working_future.cancel()
                        self.timeout_future.cancel()
                        return
            
        class Handle:
            tasks: Tasks
            client: ConferenceClient
            listener: Callable
            closed: bool
            
            def __init__(self, tasks: Tasks, client: ConferenceClient, listener: Callable) -> None:
                self.tasks = tasks
                self.client = client
                self.listener = listener
                self.closed = False
                self.client.signaling.addEventListener('connected', self._on_sig_connected)
                asyncio.ensure_future(self._on_sig_connected(None))
                
            async def _on_sig_connected(self, _):
                if self.closed:
                    return
                collector = self.client.signaling.exception_collector
                if collector:
                    await collector.add_listener(self._on_exception)
                
            def close(self):
                self.closed = True
                self.client.removeEventListener('streamadded', self.listener)
                self.tasks.close()
                self.client.signaling.removeEventListener('connected', self._on_sig_connected)
                collector = self.client.signaling.exception_collector
                if collector:
                    collector.remove_listener(self._on_exception)
                
            async def wait(self):
                try:
                    await self.tasks.wait()
                finally:
                    self.close()
                
            def renew(self) -> bool:
                return tasks.renew()
            
            def _on_exception(self, e: BaseException):
                self.tasks.set_exception(e)
            
            async def link_exception_collector(self, collector: AsyncExceptionCollector):
                await collector.add_listener(self._on_exception)
                
        tasks: Tasks = Tasks(timeout=timeout)
        async def wrap_task(stream: RemoteStream):
            if isinstance(auto_resume, bool):
                if auto_resume:
                    try_times = -1
                else:
                    try_times = 0
            else:
                try_times = auto_resume
            while True:
                try:
                    await ensure_future(callback, stream)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    raise
                except BaseException as e:
                    if stream.id not in self.remoteStreams:
                        logger.info(f'Stream {stream.id} removed, so stop the binding task.')
                        break
                    if try_times == 0:
                        raise
                    elif try_times > 0:
                        try_times = try_times - 1
                else:
                    break
                    
        def on_stream_add(evt: StreamEvent):
            tasks.add_task(asyncio.ensure_future(wrap_task(evt.stream), loop=self.__loop))
        for stream in self.remoteStreams.values():
            tasks.add_task(asyncio.ensure_future(wrap_task(stream), loop=self.__loop))
        self.addEventListener('streamadded', on_stream_add)
        handle = Handle(tasks, self, on_stream_add)
        return handle
    
    async def send(self, message: str, participantId: str = 'all'):
        """
        Send a text message to a participant or all participants.

        Args:
            message (str): Message to be sent.
            participantId (str | None): Receiver of this message. 'all' means all participants.
        """
        await self.signaling.send_text(message, participantId)
        
    def is_support_data_stream(self):
        if not self.joined():
            raise asyncio.InvalidStateError('Join a conference at first.')
        return self.quicTransportChannel is not None
        
    async def create_data_local_stream(self, attributes: dict | None = None, is_unidirectional: bool = False) -> LocalStream:
        if not self.joined():
            raise asyncio.InvalidStateError('Join a conference at first.')
        if not self.quicTransportChannel:
            raise ConferenceError('No QUIC connection available. Perhaps the conference not support quic connection.')
        return await self.quicTransportChannel.createSendStream(attributes=attributes, is_unidirectional=is_unidirectional)
        
    def clean(self):
        self.participants.clear()
        self.remoteStreams.clear()
        
    @overload
    def addEventListener(self, eventType: Literal['streamadded'], listener: Callable[[StreamEvent], Any], once: bool = False, seq: bool = True) -> None: ...
    @overload
    def addEventListener(self, eventType: Literal['messagereceived'], listener: Callable[[MessageEvent], Any], once: bool = False, seq: bool = True) -> None: ...
    @overload
    def addEventListener(self, eventType: Literal['participantjoined'], listener: Callable[[ParticipantEvent], Any], once: bool = False, seq: bool = True) -> None: ...
    def addEventListener(self, eventType: str, listener: Callable[[Any], Any], once: bool = False, seq: bool = True) -> None:
        return super().addEventListener(eventType, listener, once=once, seq=seq)
        
    async def fireMessageReceived(self, data: TextReceiveMessage):
        await self.dispatchEventAwaitable(MessageEvent('messagereceived', message=data['message'], origin=data['from'], to=data['to']))
        
    async def fireStreamAdded(self, info: StreamInfo):
        stream = self._createRemoteStream(info)
        self.remoteStreams[stream.id] = stream
        await self.dispatchEventAwaitable(StreamEvent('streamadded', stream))
        
    async def fireStreamRemoved(self, info: StreamRemoveUpdateMessage):
        if info['id'] not in self.remoteStreams:
            logger.warning(f'Cannot find specific remote stream {info["id"]}.')
            return
        stream = self.remoteStreams[info['id']]
        del self.remoteStreams[info['id']]
        await stream.dispatchEventAwaitable(OwtEvent('ended'))
        
    async def fireActiveAudioInputChange(self, info: StreamUpdateUpdateMessage):
        if info['id'] not in self.remoteStreams:
            logger.warning(f'Cannot find specific remote stream {info["id"]}.')
            return
        stream = self.remoteStreams[info['id']]
        assert info['data']['field'] == 'activeInput'
        event = ActiveAudioInputChangeEvent('activeaudioinputchange', info['data']['value'])
        await stream.dispatchEventAwaitable(event)
        
        
    async def fireLayoutChange(self, info: StreamUpdateUpdateMessage):
        if info['id'] not in self.remoteStreams:
            logger.warning(f'Cannot find specific remote stream {info["id"]}.')
            return
        stream = self.remoteStreams[info['id']]
        event = LayoutChangeEvent('layoutchange', info['data']['value'])
        await stream.dispatchEventAwaitable(event)
        
        
    async def fireParticipantEvent(self, data: ParticipantAction):
        if data['action'] == 'join':
            participant = Participant(id=data['data']['id'], role=data['data']['role'], userId=data['data']['user'])
            self.participants[participant.id] = participant
            await self.dispatchEventAwaitable(ParticipantEvent('participantjoined', participant))
        elif data['action'] == 'leave':
            participantId = data['data']
            if participantId not in self.participants:
                logger.warning(f'Received leave message from MCU for an unknown participant: {participantId}.')
                return
            participant = self.participants[participantId]
            del self.participants[participantId]
            await participant.dispatchEventAwaitable(OwtEvent('left'))

import platform

from aiortc.contrib.media import MediaPlayer


def create_local_tracks(type: Literal['camera', 'file'], path: str | None = None):
    options = {"framerate": "20", "video_size": "320x240"}
    
    if type == 'camera':
        try:
            if platform.system() == "Darwin":
                webcam = MediaPlayer(
                    "default:none", format="avfoundation", options=options
                )
            elif platform.system() == "Windows":
                # use 'ffmpeg -list_devices true -f dshow -i dummy' to get the camera name
                from device import getDeviceList
                devices = getDeviceList()
                if not devices:
                    raise ValueError('No camera found.')
                webcam = MediaPlayer(
                    f"video={devices[0][0]}", format="dshow", options=options
                )
            else:
                webcam = MediaPlayer("/dev/video0", format="v4l2", options=options)
        except BaseException as e:
            raise ValueError(f'Unable to get a camera. {e}')
    elif type == 'file':
        assert path is not None
        webcam = MediaPlayer(path, options=options)
    else:
        raise ValueError(f'invalid type {type}')
    stream = LocalStream(
        tracks=[t for t in [webcam.audio, webcam.video] if t is not None],
        quic_stream=None,
        source=StreamSourceInfo(audio='mic', video='camera'),
    )
    return stream
    