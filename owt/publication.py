from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeGuard

from .codec import (AudioCodecParameters, AudioEncodingParameters,
                    VideoCodecParameters, VideoEncodingParameters)
from .common import EventDispatcher
from .mediaformat import Resolution, TrackKink
from .transport import TransportConstraints, TransportSettings
from .webrtc import RTCRtpEncodingParameters


@dataclass
class AudioPublicationSettings:
    codec: AudioCodecParameters | None = None
    _trackId: str | None = None
    
@dataclass
class VideoPublicationSettings:
    codec: VideoCodecParameters | None = None
    resolution: Resolution | None = None
    frameRate: float | None = None
    bitrate: float | str | None = None
    keyFrameInterval: float | None = None
    rid: str | None = None
    _trackId: str | None = None

@dataclass
class PublicationSettings:
    audio: list[AudioPublicationSettings] = field(default_factory=list)
    video: list[VideoPublicationSettings] = field(default_factory=list)

class Publication(EventDispatcher):
    id: str
    transport: TransportSettings
    stop: Callable[[], Awaitable[Any]]
    mute: Callable[[TrackKink], Awaitable[Any]]
    unmute: Callable[[TrackKink], Awaitable[Any]]
    
    def __init__(
        self, id: str,
        transport: TransportSettings,
        stop: Callable[[], Any],
        mute: Callable[[TrackKink], Any],
        unmute: Callable[[TrackKink], Any],
    ) -> None:
        super().__init__()
        self.id = id
        self.transport = transport
        self.stop = stop
        self.mute = mute
        self.unmute = unmute
   
@dataclass
class PublishOptions:
    """
    PublishOptions defines options for publishing a LocalStream.
    """
    
    audio: list[AudioEncodingParameters] | list[RTCRtpEncodingParameters] | bool | None = None
    """
    Parameters for audio RtpSender.
    """
    video: list[VideoEncodingParameters] | list[RTCRtpEncodingParameters] | bool | None = None
    """
    Parameters for video RtpSender.
    """
    transport: TransportConstraints | None = None
    
    def __post_init__(self):
        if (self.isAudioEncodingParameters(self.audio) and self.isRTCRtpEncodingParameters(self.video)) or (self.isVideoEncodingParameters(self.video) and self.isRTCRtpEncodingParameters(self.audio)):
            raise TypeError('Mixing RTCRtpEncodingParameters and AudioEncodingParameters/VideoEncodingParameters is not allowed.')
        
    @staticmethod
    def isAudioEncodingParameters(value: list[AudioEncodingParameters] | list[RTCRtpEncodingParameters] | bool | None) -> TypeGuard[list[AudioEncodingParameters]]:
        if isinstance(value, list):
            if len(value) > 0:
                return isinstance(value[0], AudioEncodingParameters)
        return False
    
    @staticmethod
    def isVideoEncodingParameters(value: list[VideoEncodingParameters] | list[RTCRtpEncodingParameters] | bool | None) -> TypeGuard[list[VideoEncodingParameters]]:
        if isinstance(value, list):
            if len(value) > 0:
                return isinstance(value[0], VideoEncodingParameters)
        return False
    
    @staticmethod
    def isRTCRtpEncodingParameters(value: list[AudioEncodingParameters] | list[VideoEncodingParameters] | list[RTCRtpEncodingParameters] | bool | None) -> TypeGuard[list[RTCRtpEncodingParameters]]:
        if isinstance(value, list):
            if len(value) > 0:
                return isinstance(value[0], RTCRtpEncodingParameters)
        return False
    