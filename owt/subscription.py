from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiortc import MediaStreamTrack

from .common import EventDispatcher
from .mediaformat import Resolution, TrackKink
from .sub_types import (AudioSubscriptionConstraints,
                        VideoSubscriptionConstraints)
from .transport import TransportConstraints, TransportSettings
from .quic_types import QuicBidirectionalStream


@dataclass
class SubscribeOptions:
    audio: AudioSubscriptionConstraints | bool | None = None
    video: VideoSubscriptionConstraints | bool | None = None
    transport: TransportConstraints | None = None
    
@dataclass
class VideoSubscriptionUpdateOptions:
    resolution: Resolution | None = None
    frameRate: float | None = None
    bitrateMultiplier: float | None = None
    keyFrameInterval: float | None = None

@dataclass    
class SubscriptionUpdateOptions:
    video: VideoSubscriptionUpdateOptions | None = None

class Subscription(EventDispatcher):
    id: str
    tracks: list[MediaStreamTrack]
    quic_stream: QuicBidirectionalStream | None
    transport: TransportSettings
    stop: Callable[[], Awaitable[Any]]
    mute: Callable[[TrackKink], Awaitable[Any]]
    unmute: Callable[[TrackKink], Awaitable[Any]]
    _audioTrackId: str | None
    _videoTrackId: str | None
    
    
    def __init__(
        self, id: str,
        track: list[MediaStreamTrack],
        quic_stream: QuicBidirectionalStream | None,
        transport: TransportSettings,
        stop: Callable[[], Any],
        mute: Callable[[TrackKink], Any],
        unmute: Callable[[TrackKink], Any],
    ) -> None:
        super().__init__()
        self.id = id
        self.tracks = track
        self.quic_stream = quic_stream
        self.transport = transport
        self.stop = stop
        self.mute = mute
        self.unmute = unmute
        
    def audio_track(self) -> MediaStreamTrack | None:
        return next((t for t in self.tracks if t.kind == 'audio'), None)
    
    def video_track(self) -> MediaStreamTrack | None:
        return next((t for t in self.tracks if t.kind == 'video'), None)
    
    def tracks_ready(self) -> bool:
        return len(self.tracks) == len(self.transport.rtpTransceivers)
    
    def data_stream(self):
        if self.transport.type != 'quic':
            raise ConnectionError('Only data subscription has data stream atribute.')
        assert self.quic_stream
        return self.quic_stream
        
    

    