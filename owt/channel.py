import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import cast

from aiortc import (MediaStreamTrack, RTCConfiguration, RTCPeerConnection,
                    RTCRtpTransceiver, RTCSessionDescription)

if sys.version_info < (3, 11):
    from typing_extensions import Any, Literal
else:
    from typing import Any, Literal

from .common import (AudioTransceiver, ConferenceError, ErrorEvent,
                     EventDispatcher, MessageEvent, MuteEvent, OwtEvent,
                     Transceiver, VideoParameters, VideoTransceiver)
from .constants import AudioCodec, VideoCodec
from .mediaformat import TrackKink
from .portal_types import (CandidateMessage, OfferAnswer, StreamUpdateMessage,
                           TransportProgress)
from .publication import Publication, PublishOptions
from .sdputils import reorderCodecs, setMaxBitrate
from .signaling import SioSignaling
from .stream import LocalStream, RemoteStream
from .subscription import (AudioSubscriptionConstraints, SubscribeOptions,
                           Subscription, SubscriptionUpdateOptions,
                           VideoSubscriptionConstraints)
from .transport import TransportSettings
from .utils import AsyncExceptionCollector, chain_future, remove_none_value, Postponer

logger = logging.getLogger("OWT")
logger.setLevel(logging.DEBUG)


@dataclass
class TransceiversWithId:
    id: str | None = None
    transceivers: list[Transceiver] = field(default_factory=list)

class OwtChannel(EventDispatcher):
    pc: RTCPeerConnection
    _lock: asyncio.Lock
    _signaling: SioSignaling
    _id: str | None # transportId
    _remoteId: str | None
    _internalCount: int
    _ended: bool
    _subscriptions: dict[str, Subscription] # SubscriptionId => Subscription
    _subscribeTransceivers: dict[int, TransceiversWithId] # internalId => { id, transceivers: [Transceiver] }
    _subscribePromises: dict[int, asyncio.Future[Subscription]] # internalId => Future
    _reverseIdMap: dict[str, int] # PublicationId || SubscriptionId => internalId
    _remoteMediaTracks: dict[str, list[MediaStreamTrack]] # Key is subscription ID, value is MediaStreamTrack.
    _publications: dict[str, Publication] # PublicationId => Publication
    _publishTransceivers: dict[int, TransceiversWithId] # internalId => { id, transceivers: [Transceiver] }
    _publishPromises: dict[int, asyncio.Future[Publication]] # internalId => Future
    _sdpPromise: asyncio.Future[None]
    _sdpResolvers: list[asyncio.Future[None]]
    _sdpResolverMap: dict[int, asyncio.Future[None]]
    _sdpResolveNum: int
    _loop: asyncio.AbstractEventLoop | None
    exception_collector: AsyncExceptionCollector
    
    def __init__(self, signaling: SioSignaling, rtc_configure: RTCConfiguration | None = None, loop: asyncio.AbstractEventLoop | None = None) -> None:
        super().__init__()
        self._loop = loop
        self._lock = asyncio.Lock()
        self._ended = False
        self._id = None
        self._remoteId = None
        self._internalCount = 0
        self._signaling = signaling
        self._subscriptions = {}
        self._subscribeTransceivers = {}
        self._subscribePromises = {}
        
        self._reverseIdMap = {} # PublicationId || SubscriptionId => internalId
        
        self._remoteMediaTracks = {}
        
        self._publications = {}
        self._publishTransceivers = {}
        self._publishPromises = {}
        
        self._sdpPromise = asyncio.Future(loop=self._loop)
        self._sdpPromise.set_result(None)
        self._sdpResolvers = []
        self._sdpResolverMap = {}
        self._sdpResolveNum = 0
        self.exception_collector = AsyncExceptionCollector()
        self.pc = RTCPeerConnection(configuration=rtc_configure)
        self.pc.on('track', self._onRemoteTrackAdded)
        self.pc.on('iceconnectionstatechange', self._onIceConnectionStateChange)
        self.pc.on('connectionstatechange', self._onConnectionStateChange)
        def on_signalingstatechange():
            logger.debug(f'signaling state: {self.pc.signalingState}')
        self.pc.on('signalingstatechange', on_signalingstatechange)
        def on_icegatheringstatechange():
            logger.debug(f'ice gathering state: {self.pc.iceGatheringState}')
        self.pc.on('icegatheringstatechange', on_icegatheringstatechange)
        
    async def onMessage(self, notification: str, message: Any):
        async with self.exception_collector.collect():
            async with await Postponer().postpone(self, self.close):
                if notification == 'progress':
                    progress_message = cast(TransportProgress, message)
                    if progress_message['status'] == 'soac':
                        await self._sdpHandler(progress_message['data'])
                    elif progress_message['status'] == 'ready' and 'sessionId' in progress_message:
                        # ready消息可能发两次，第一次不带sessionId，只有id，也就是transport id，用以建立连接，第二次才带sessionId
                        sessionId = progress_message['sessionId']
                        assert sessionId is not None
                        self._readyHandler(sessionId)
                    elif progress_message['status'] == 'error':
                        # 文档中完美没提到sessionId字段，但js源码里用的是sessionId字段
                        await self._errorHandler(progress_message.get('sessionId'), progress_message['data'])
                elif notification == 'stream':
                    stream_message = cast(StreamUpdateMessage, message)
                    await self._onStreamEvent(stream_message)
                else:
                    logger.warning('Unknown notification from MCU.')
            
    async def publishWithTransceivers(self, stream: LocalStream, transceivers: list[RTCRtpTransceiver]) -> Publication:
        for t in transceivers:
            if t.direction != 'sendonly':
                raise ConferenceError('RTCRtpTransceiver\'s direction must be sendonly.')
            if t.sender.track not in stream.tracks:
                raise ConferenceError('The track associated with RTCRtpSender is not included in stream.')
            if len(transceivers) > 2:
                raise ConferenceError('At most one transceiver for audio and one transceiver for video are accepted.')
        internalId = self._createInternalId()
        await self._chainSdpPromise(internalId)
        transceiverDescription = [
            (VideoTransceiver(type='video', transceiver=t, source=stream.source.video, option=[]) if t.sender.track.kind == 'video'
            else AudioTransceiver(type='audio', transceiver=t, source=stream.source.audio, option=[])) for t in transceivers]
        self._publishTransceivers[internalId] = TransceiversWithId(transceivers=transceiverDescription)
        future: asyncio.Future[Publication] = asyncio.Future(loop=self._loop)
        self._publishPromises[internalId] = future
        try:
            localDesc = await self.pc.createOffer()
            await self.pc.setLocalDescription(localDesc)
            localDesc = self.pc.localDescription
            pub_res = await self._signaling.publish({
                'media': { 'tracks': [{ 'type': t.type, 'mid': t.transceiver.mid, 'source': t.source } for t in transceiverDescription] },
                'attributes': stream.attributes,
                'transport': {
                    'id': self._id,
                    'type': 'webrtc',
                },
            })
            publicationId = pub_res['id']
            self._publishTransceivers[internalId].id = publicationId
            self._reverseIdMap[publicationId] = internalId
            if self._id and self._id != pub_res['transportId']:
                raise ConferenceError(f'Server returns conflict ID: {pub_res["transportId"]}')
            self._id = pub_res['transportId']
            await self.dispatchEventAwaitable(MessageEvent('id', message=publicationId, origin=self._remoteId))
            await self._signaling.soac(
                self._id,
                {
                    'type': cast(Literal['offer'], localDesc.type),
                    'sdp': localDesc.sdp,
                },
            )
            return await future
        except:
            await self._unpublish(internalId)
            raise
            
    async def publish(self, stream: LocalStream, options: PublishOptions | list[RTCRtpTransceiver] | None, videoCodecs: list[str] | None) -> Publication:
        if self._ended:
            raise ConferenceError('Connection closed')
        if isinstance(options, list):
            return await self.publishWithTransceivers(stream, options)
        if options is None:
            options = PublishOptions(
                audio=stream.hasAudio(),
                video=stream.hasVideo(),
            )
        if options.audio is None:
            options.audio = stream.hasAudio()
        if options.video is None:
            options.video = stream.hasVideo()
        if (options.audio and not stream.hasAudio()) or (options.video and not stream.hasVideo()):
            raise ConferenceError('options.audio/video is inconsistent with tracks presented in the MediaStream')
        if not options.audio and not options.video:
            raise ConferenceError('Cannot publish a stream without audio and video.')
        if stream.audio_num > 1:
            logger.warning('Publishing a stream with multiple audio tracks is not fully supported.')
        if stream.video_num > 1:
            logger.warning('Publishing a stream with multiple video tracks is not fully supported.')
        
        internalId = self._createInternalId()
        await self._chainSdpPromise(internalId)
        transceivers: list[Transceiver] = []
        if stream.hasVideo():
            track = stream.videoTrack
            assert track is not None
            transceiver = self.pc.addTransceiver(track, direction='sendonly')
            transceivers.append(VideoTransceiver(
                type='video',
                transceiver=transceiver,
                source=stream.source.video,
                option=options.video,
            ))
        if stream.hasAudio():
            track = stream.audioTrack
            assert track is not None
            transceiver = self.pc.addTransceiver(track, direction='sendonly')
            transceivers.append(AudioTransceiver(
                type='audio',
                transceiver=transceiver,
                source=stream.source.audio,
                option=options.audio,
            ))
        assert internalId not in self._publishTransceivers
        self._publishTransceivers[internalId] = TransceiversWithId(transceivers=transceivers)
        future: asyncio.Future[Publication] = asyncio.Future(loop=self._loop)
        self._publishPromises[internalId] = future
        publicationId = None
        try:
            localDesc = await self.pc.createOffer()
            await self.pc.setLocalDescription(localDesc)
            localDesc = self.pc.localDescription
            pub_res = await self._signaling.publish({
                'media': { 'tracks': [{ 'type': t.type, 'mid': t.transceiver.mid, 'source': t.source } for t in transceivers] },
                'attributes': stream.attributes,
                'transport': {
                    'id': self._id or '',
                    'type': 'webrtc',
                },
            })
            publicationId = pub_res['id']
            self._publishTransceivers[internalId].id = publicationId
            self._reverseIdMap[publicationId] = internalId
            if self._id and self._id != pub_res['transportId']:
                raise ConferenceError(f'Server returns conflict ID: {pub_res["transportId"]}')
            self._id = pub_res['transportId']
            await self.dispatchEventAwaitable(MessageEvent('id', message=publicationId, origin=self._remoteId))
            if options:
                for t in transceivers:
                    localDesc.sdp = self._setRtpReceiverOptions(localDesc.sdp, options, videoCodecs, t.transceiver.mid)
                    localDesc.sdp = self._setRtpSenderOptions(localDesc.sdp, options, t.transceiver.mid)
            await self._signaling.soac(
                self._id,
                {
                    'type': cast(Literal['offer'], localDesc.type),
                    'sdp': localDesc.sdp,
                },
            )
            return await future
        except:
            await self._unpublish(internalId)
            raise
    
    async def subscribe(self, stream: RemoteStream, options: SubscribeOptions | None = None) -> Subscription:
        if self._ended:
            raise asyncio.InvalidStateError('Connection closed')
        has_audio_stream = stream.settings and stream.settings.audio is not None
        has_video_stream = stream.settings and stream.settings.video is not None
        if not options:
            options = SubscribeOptions(audio=has_audio_stream, video=has_video_stream)
        if not options.audio:
            options.audio = has_audio_stream
        if not options.video:
            options.video = has_video_stream
        if (options.audio and not has_audio_stream) or (options.video and not has_video_stream):
            raise ValueError('options.audio/video cannot be true or an object if there is no audio/video track in remote stream.')
        if not options.audio and not options.video:
            raise ValueError('Cannot subscribe a stream without audio and video.')
        if options.audio and isinstance(options.audio, AudioSubscriptionConstraints):
            if not options.audio.codecs:
                raise ValueError('Audio codec cannot be none or an empty array.')
        if options.video and isinstance(options.video, VideoSubscriptionConstraints):
            if not options.video.codecs:
                raise ValueError('Video codec cannot be none or an empty array.')
        internalId = self._createInternalId()
        await self._chainSdpPromise(internalId)
        transceivers: list[Transceiver] = []
        if options.video:
            from_id = stream.id
            parameters = None
            if isinstance(options.video, VideoSubscriptionConstraints):
                if not options.video.rid:
                    if options.video.resolution or options.video.frameRate or (options.video.bitrateMultiplier and options.video.bitrateMultiplier != 1) or options.video.keyFrameInterval:
                        parameters = VideoParameters(
                            resolution=options.video.resolution,
                            framerate=options.video.frameRate,
                            bitrate=f'x{options.video.bitrateMultiplier}' if options.video.bitrateMultiplier else None,
                            keyFrameInterval=options.video.keyFrameInterval,
                        )
                else:
                    assert stream.settings and stream.settings.video
                    matched = next((v for v in stream.settings.video if v.rid == options.video.rid), None)
                    if matched and matched._trackId:
                        from_id = matched._trackId
                        parameters = None
            transceiver = self.pc.addTransceiver('video', direction='recvonly')
            transceivers.append(VideoTransceiver(
                type='video',
                transceiver=transceiver,
                from_id=from_id,
                parameters=parameters,
                option=options.video,
            ))
        # audio must after video, or pc connect hang.
        if options.audio:
            transceiver = self.pc.addTransceiver('audio', direction='recvonly')
            transceivers.append(AudioTransceiver(
                type='audio',
                transceiver=transceiver,
                from_id=stream.id,
                option=options.audio,
            ))
        assert internalId not in self._subscribeTransceivers
        self._subscribeTransceivers[internalId] = TransceiversWithId(transceivers=transceivers)
        future: asyncio.Future[Subscription] = asyncio.Future(loop=self._loop)
        self._subscribePromises[internalId] = future
        subscriptionId = None
        try:
            await self.pc.setLocalDescription(await self.pc.createOffer())
            sub_res = await self._signaling.subscribe(remove_none_value({
                'media': {
                    'tracks': [{
                        'type': t.type,
                        'mid': t.transceiver.mid,
                        'from': t.from_id,
                        'parameters': t.parameters.to_dict() if t.parameters else None,
                    } for t in transceivers]
                },
                'transport': {
                    'id': self._id,
                    'type': 'webrtc',
                }
            }, recursion=True))
            subscriptionId = sub_res['id']
            self._subscribeTransceivers[internalId].id = subscriptionId
            self._reverseIdMap[subscriptionId] = internalId
            if self._id and self._id != sub_res['transportId']:
                raise ConferenceError(f'Server returns conflict transport ID: {sub_res["transportId"]}')
            self._id = sub_res['transportId']
            await self.dispatchEventAwaitable(MessageEvent('id', message=subscriptionId, origin=self._remoteId))
            if options:
                for t in transceivers:
                    self.pc.localDescription.sdp = self._setRtpReceiverOptions(self.pc.localDescription.sdp, options, mid=t.transceiver.mid)
            await self._signaling.soac(
                self._id,
                {
                    'type': cast(Literal['offer'], self.pc.localDescription.type),
                    'sdp': self.pc.localDescription.sdp,
                },
            )
            return await future
        except:
            await self._unsubscribe(internalId)
            raise
        
    def _onRemoteTrackAdded(self, track: MediaStreamTrack):
        logger.debug('Remote stream added.')
        for internalId, sub in self._subscribeTransceivers.items():
            if next((t for t in sub.transceivers if t.transceiver.receiver.track == track), None):
                assert sub.id is not None
                if sub.id in self._subscriptions:
                    subscription = self._subscriptions[sub.id]
                    subscription.tracks.append(track)
                    if subscription.tracks_ready() and internalId in self._subscribePromises:
                        self._subscribePromises[internalId].set_result(subscription)
                        del self._subscribePromises[internalId]
                else:
                    if sub.id not in self._remoteMediaTracks:
                        tracks = self._remoteMediaTracks[sub.id] = []
                    else:
                        tracks = self._remoteMediaTracks[sub.id]
                    tracks.append(track)
            return
            # This is not expected path. However, this is going to happen on Safari
            # because it does not support setting direction of transceiver.
        logger.warning('Received remote stream without subscription.')
        
    async def _onIceConnectionStateChange(self):
        logger.debug(f'ICE connection state changed to {self.pc.iceConnectionState}')
        async with self.exception_collector.collect():
            if self.pc.iceConnectionState == 'closed' or self.pc.iceConnectionState == 'failed':
                if self.pc.iceConnectionState == 'failed':
                    await self._handleError('connection failed.')
                else:
                    # Fire ended event if publication or subscription exists.
                    await self.close()
                
    async def _onConnectionStateChange(self):
        logger.debug(f'Connection state changed to {self.pc.connectionState}')
        async with self.exception_collector.collect():
            if self.pc.connectionState == 'closed' or self.pc.connectionState == 'failed':
                if self.pc.connectionState == 'failed':
                    await self._handleError('connection failed.')
                else:
                    # Fire ended event if publication or subscription exists.
                    await self.close()
        
    async def close_peer(self):
        if self.pc and self.pc.signalingState != 'closed':
            await self.pc.close()
        
    async def _chainSdpPromise(self, internalId: int):
        prior = self._sdpPromise
        negotiationTimeout = 10
        future = asyncio.Future(loop=self._loop)
        async def prepare_sdpResolver():
            self._sdpResolvers.append(future)
            assert internalId not in self._sdpResolverMap
            self._sdpResolverMap[internalId] = future
            return await future
        async def then(_):
            try:
                await asyncio.wait_for(prepare_sdpResolver(), timeout=negotiationTimeout)
            except asyncio.TimeoutError:
                raise ConferenceError('Timeout to get SDP answer')
        self._sdpPromise = chain_future(prior, then)
        return await chain_future(prior, catch=lambda _: None)
    
    def _nextSdpPromise(self):
        ret = False
        while self._sdpResolveNum < len(self._sdpResolvers):
            resolver = self._sdpResolvers[self._sdpResolveNum]
            self._sdpResolveNum += 1
            if not resolver.done():
                resolver.set_result(None)
                ret = True
                
        return ret
    
    def _createInternalId(self):
        id = self._internalCount
        self._internalCount += 1
        return id
        
    async def _unpublish(self, internalId: int):
        if internalId in self._publishTransceivers:
            transceivers_with_id = self._publishTransceivers[internalId]
            id = transceivers_with_id.id
            transceivers = transceivers_with_id.transceivers
            if id is not None:
                try:
                    await self._signaling.unpublish(id)
                except Exception as e:
                    logger.warning(f'MCU returns negative ack for unpublishing, {e}')
                if id in self._reverseIdMap:
                    del self._reverseIdMap[id]
            for transceiver in transceivers:
                if self.pc.signalingState == 'stable':
                    transceiver.transceiver.sender.replaceTrack(None)
                    # TODO: remove the transceiver
            del self._publishTransceivers[internalId]
            if id in self._publications:
                event = OwtEvent('ended')
                await self._publications[id].dispatchEventAwaitable(event)
                del self._publications[id]
            else:
                logger.warning(f'Invalid publication to unpublish: {id}')
                future = self._publishPromises.get(internalId)
                if future is not None:
                    if not future.done():
                        future.set_exception(ConferenceError('Failed to publish'))
                    del self._publishPromises[internalId]
            future = self._sdpResolverMap.get(internalId)
            if future is not None:
                if not future.done():
                    future.set_result(None)
                del self._sdpResolverMap[internalId]
                # not del from _sdpResolvers here
            
        
    async def _unsubscribe(self, internalId: int):
        if internalId in self._subscribeTransceivers:
            transceivers_with_id = self._subscribeTransceivers[internalId]
            id = transceivers_with_id.id
            transceivers = transceivers_with_id.transceivers
            if id is not None:
                try:
                    await self._signaling.unsubscribe(id)
                except Exception as e:
                    logger.warning(f'MCU returns negative ack for unsubscribing, {e}')
                if id in self._reverseIdMap:
                    del self._reverseIdMap[id]
            for transceiver in transceivers:
                if transceiver.transceiver.receiver and transceiver.transceiver.receiver.track:
                    transceiver.transceiver.receiver.track.stop()
            del self._subscribeTransceivers[internalId]
            if id in self._subscriptions:
                event = OwtEvent('ended')
                await self._subscriptions[id].dispatchEventAwaitable(event)
                del self._subscriptions[id]
            else:
                logger.warning(f'Invalid subscription to unsubscribe: {id}')
                future = self._subscribePromises.get(internalId)
                if future is not None:
                    if not future.done():
                        future.set_exception(ConferenceError('Failed to subscribe'))
                    del self._subscribePromises[internalId]
            future = self._sdpResolverMap.get(internalId)
            if future is not None:
                if not future.done():
                    future.set_result(None)
                del self._sdpResolverMap[internalId]
                
    async def _muteOrUnmute(self, sessionId: str, isMute: bool, isPub: bool, trackKind: TrackKink):
        event_name = 'stream-control' if isPub else 'subscription-control'
        operation = 'pause' if isMute else 'play'
        await self._signaling.ready()
        await self._signaling.send(event_name, { 'id': sessionId, 'operation': operation, 'data': trackKind })
        if not isPub:
            mute_event_name = 'mute' if isMute else 'unmute'
            subscription = self._subscriptions.get(sessionId)
            assert subscription is not None
            await subscription.dispatchEventAwaitable(MuteEvent(mute_event_name, trackKind))
                      
        
    def _readyHandler(self, sessionId: str):
        internalId = self._reverseIdMap.get(sessionId)
        if internalId in self._subscribePromises:
            try:
                assert internalId in self._subscribeTransceivers
                tracks = self._remoteMediaTracks.get(sessionId, [])
                transceivers = self._subscribeTransceivers[internalId].transceivers
                assert self._id is not None
                transportSettings = TransportSettings('webrtc', self._id, transceivers)
                subscription = Subscription(
                    sessionId,
                    tracks,
                    None,
                    transportSettings,
                    lambda: self._unsubscribe(internalId),
                    lambda kind: self._muteOrUnmute(sessionId, True, False, kind),
                    lambda kind: self._muteOrUnmute(sessionId, False, False, kind),
                )
                assert sessionId not in self._subscriptions
                self._subscriptions[sessionId] = subscription
                del self._remoteMediaTracks[sessionId]
                if subscription.tracks_ready():
                    self._subscribePromises[internalId].set_result(subscription)
                    del self._subscribePromises[internalId]
            except BaseException as e:
                self._subscribePromises[internalId].set_exception(e)
                del self._subscribePromises[internalId]
        elif internalId in self._publishPromises:
            try:
                assert internalId in self._publishTransceivers
                transceivers = self._publishTransceivers[internalId].transceivers
                assert self._id is not None
                transportSettings = TransportSettings('webrtc', self._id, transceivers)
                publication = Publication(
                    sessionId,
                    transportSettings,
                    lambda: self._unpublish(internalId),
                    lambda kind: self._muteOrUnmute(sessionId, True, True, kind),
                    lambda kind: self._muteOrUnmute(sessionId, False, True, kind),
                )
                assert sessionId not in self._publications
                self._publications[sessionId] = publication
                self._publishPromises[internalId].set_result(publication)
            except BaseException as e:
                self._publishPromises[internalId].set_exception(e)
            # Do not fire publication's ended event when associated stream is ended.
            # It may still sending silence or black frames.
            # Refer to https://w3c.github.io/webrtc-pc/#rtcrtpsender-interface.
        elif not sessionId:
            # Channel ready
            pass
        
        
    async def _sdpHandler(self, sdp: OfferAnswer | CandidateMessage):
        if sdp['type'] == 'answer':
            sdp_sdp = sdp.get('sdp')
            assert sdp_sdp is not None
            _sdp = RTCSessionDescription(sdp_sdp, sdp['type'])
            try:
                logger.debug(sdp_sdp)
                await self.pc.setRemoteDescription(_sdp)
            except Exception as e:
                logger.error(f'Set remote description failed: {e}')
                await self._rejectPromise(e)
                await self.close()
            if not self._nextSdpPromise():
                logger.warning('Unexpected SDP promise state')
                
    async def _errorHandler(self, sessionId: str | None, errorMessage: str):
        if not sessionId:
            return await self._handleError(errorMessage)
        errorEvent = ErrorEvent('error', ConferenceError(errorMessage))
        if sessionId in self._publications:
            await self._publications[sessionId].dispatchEventAwaitable(errorEvent)
        if sessionId in self._subscriptions:
            await self._subscriptions[sessionId].dispatchEventAwaitable(errorEvent)
        internalId = self._reverseIdMap.get(sessionId)
        if internalId in self._publishTransceivers:
            await self._unpublish(internalId)
        if internalId in self._subscribeTransceivers:
            await self._unsubscribe(internalId)
        
            
    async def _handleError(self, errorMessage: str):
        if self._ended:
            return
        error = ConferenceError(errorMessage)
        event = ErrorEvent('error', error=error)
        for publication in self._publications.values():
            await publication.dispatchEventAwaitable(event)
        for subscription in self._subscriptions.values():
            await subscription.dispatchEventAwaitable(event)
        await self.close()
        
    def _getAudioCodecs(self, options: PublishOptions | SubscribeOptions | SubscriptionUpdateOptions) -> list[AudioCodec]:
        if isinstance(options, PublishOptions):
            audio = options.audio
            if PublishOptions.isAudioEncodingParameters(audio): 
                return [p.codec.name for p in audio if p.codec is not None]
        elif isinstance(options, SubscribeOptions):
            audio = options.audio
            if audio and not isinstance(audio, bool) and audio.codecs is not None:
                return [codec.name for codec in audio.codecs]
        return []
    
    def _getVideoCodecs(self, options: PublishOptions | SubscribeOptions | SubscriptionUpdateOptions) -> list[VideoCodec]:
        if isinstance(options, PublishOptions):
            video = options.video
            if PublishOptions.isVideoEncodingParameters(video):
                return [p.codec.name for p in video if p.codec is not None]
        elif isinstance(options, SubscribeOptions):
            video = options.video
            if video and not isinstance(video, bool) and video.codecs is not None:
                return [codec.name for codec in video.codecs]
        return []
        
    def _setCodecOrder(self, sdp: str, options: PublishOptions | SubscribeOptions | SubscriptionUpdateOptions, mid: str | None = None) -> str:
        codecs = self._getAudioCodecs(options)
        if codecs:
            sdp = reorderCodecs(sdp, 'audio', cast(list[str], codecs), mid)
        codecs = self._getVideoCodecs(options)
        if codecs:
            sdp = reorderCodecs(sdp, 'video', cast(list[str], codecs), mid)
        return sdp
    
    def _setMaxBitrate(self, sdp: str, options: PublishOptions | SubscribeOptions | SubscriptionUpdateOptions, mid: str | None = None) -> str:
        if not isinstance(options, SubscriptionUpdateOptions) and isinstance(options.audio, list) and PublishOptions.isAudioEncodingParameters(options.audio):
            sdp = setMaxBitrate(sdp, options.audio, mid)
        if isinstance(options.video, list) and PublishOptions.isVideoEncodingParameters(options.video):
            sdp = setMaxBitrate(sdp, options.video, mid)
        return sdp
            
    def _setRtpSenderOptions(self, sdp: str, options: PublishOptions | SubscribeOptions | SubscriptionUpdateOptions, mid: str | None = None) -> str:
        return self._setMaxBitrate(sdp, options, mid)
    
    def _setRtpReceiverOptions(self, sdp: str, options: PublishOptions | SubscribeOptions | SubscriptionUpdateOptions, video_codecs: list[str] | None = None, mid: str | None = None) -> str:
        if isinstance(options, PublishOptions):
            if PublishOptions.isRTCRtpEncodingParameters(options.video) and video_codecs:
                sdp = reorderCodecs(sdp, 'video', video_codecs, mid)
                return sdp
            if PublishOptions.isRTCRtpEncodingParameters(options.audio) or PublishOptions.isRTCRtpEncodingParameters(options.video):
                return sdp
        sdp = self._setCodecOrder(sdp, options, mid)
        return sdp    
        
    @Postponer.enchance
    async def close(self):
        if self._ended:
            return
        async with self.exception_collector.collect():
            self._ended = True
            publications = list(self._publications.values())
            for publication in publications:
                await publication.stop()
            subscriptions = list(self._subscriptions.values())
            for subscription in subscriptions:
                await subscription.stop()
            await self.dispatchEventAwaitable(OwtEvent('ended'))
            await self.close_peer()
            self.exception_collector.complete()
        
    async def _rejectPromise(self, error: Exception | None):
        if not error:
            error = ConferenceError('Connection failed or closed.')
        for promise in self._publishPromises.values():
            promise.set_exception(error)
        self._publishPromises = {}
        for promise in self._subscribePromises.values():
            promise.set_exception(error)
        self._subscribePromises = {}
        
    async def _onStreamEvent(self, message: StreamUpdateMessage):
        eventTargets: list[EventDispatcher] = []
        msg_id = message['id']
        if msg_id in self._publications:
            eventTargets.append(self._publications[msg_id])
        for subscription in self._subscriptions.values():
            # 
            if msg_id == subscription._audioTrackId or msg_id == subscription._videoTrackId:
                eventTargets.append(subscription)
        if len(eventTargets) == 0:
            logger.warning('this should not happen.')
            return
        if message['status'] != 'update':
            return
        if message['data']['field'] == 'audio.status':
            trackKind: TrackKink = 'audio'
        elif message['data']['field'] == 'video.status':
            trackKind: TrackKink = 'video'
        else:
            logger.warning('Invalid data field for stream update info.')
            return
        if message['data']['value'] == 'active':
            for target in eventTargets:
                await target.dispatchEventAwaitable(MuteEvent('unmute', trackKind))
        elif message['data']['value'] == 'inactive':
            for target in eventTargets:
                await target.dispatchEventAwaitable(MuteEvent('mute', trackKind))
        