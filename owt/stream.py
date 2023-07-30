import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, cast, overload

from aiortc import MediaStreamTrack

from .common import EventDispatcher, OwtEvent
from .constants import AudioCodec, AudioSourceInfo, VideoCodec, VideoSourceInfo
from .portal_types import MediaInfo, StreamInfo
from .publication import (AudioCodecParameters, AudioPublicationSettings,
                          PublicationSettings, Resolution,
                          VideoCodecParameters, VideoPublicationSettings)
from .sub_types import (AudioSubscriptionCapabilities,
                        SubscriptionCapabilities,
                        VideoSubscriptionCapabilities)
from .quic_types import QuicBidirectionalStream

logger = logging.getLogger('owt[stream]')

@dataclass
class StreamSourceInfo:
    """
    Audio source info or video source info could be undefined if a stream does not have audio/video track.
    """
    
    audio: AudioSourceInfo | None = None
    """
    Audio source info. Accepted values are: "mic", "screen-cast", "file", "mixed" or undefined.
    """
    video: VideoSourceInfo | None = None
    """
    Video source info. Accepted values are: "camera", "screen-cast", "file", "mixed" or undefined.
    """
    data: bool = False
    """
    Indicates whether it is data. Accepted values are boolean.
    """

@dataclass
class Stream(EventDispatcher):
    tracks: list[MediaStreamTrack]
    quic_stream: QuicBidirectionalStream | None
    source: StreamSourceInfo
    attributes: dict = field(default_factory=dict)
    
    def __post_init__(self):
        super().__init__()
    
    def hasAudio(self) -> bool:
        for track in self.tracks:
            if track.kind == 'audio':
                return True
        return False
    
    def hasVideo(self) -> bool:
        for track in self.tracks:
            if track.kind == 'video':
                return True
        return False
    
    def hasData(self) -> bool:
        return self.quic_stream is not None
    
    @property
    def audio_num(self) -> int:
        i = 0
        for track in self.tracks:
            if track.kind == 'audio':
                i += 1
        return i
    
    @property
    def audioTracks(self) -> list[MediaStreamTrack]:
        return [track for track in self.tracks if track.kind == 'audio']
    
    @property
    def audioTrack(self) -> MediaStreamTrack | None:
        for track in self.tracks:
            if track.kind == 'audio':
                return track
        return None
    
    @property
    def video_num(self) -> int:
        i = 0
        for track in self.tracks:
            if track.kind == 'video':
                i += 1
        return i
    
    @property
    def videoTracks(self) -> list[MediaStreamTrack]:
        return [track for track in self.tracks if track.kind == 'video']
    
    @property
    def videoTrack(self) -> MediaStreamTrack | None:
        for track in self.tracks:
            if track.kind == 'video':
                return track
        return None
    
    @property
    def data_stream(self) -> QuicBidirectionalStream:
        assert self.quic_stream, f'Only data stream has quic stream.'
        return self.quic_stream

@dataclass
class LocalStream(Stream):
    id: str = field(default_factory=lambda: f'{uuid.uuid1()}')
    
@dataclass
class RemoteStream(Stream):
    id: str = field(default_factory=lambda: f'{uuid.uuid1()}')
    origin: str | None = None
    """
    ID of the remote endpoint who published this stream.
    """
    settings: PublicationSettings | None = None
    """
    Original settings for publishing this stream. This property is only valid in conference mode.
    """
    extraCapabilities: SubscriptionCapabilities | None = None
    """
    Extra capabilities remote endpoint provides for subscription. Extra
    capabilities don't include original settings. This property is only valid
    in conference mode.
    """
    
    @overload
    def addEventListener(self, eventType: Literal['ended'], listener: Callable[[OwtEvent], Any], once: bool = False, seq: bool = True) -> None: ...
    @overload
    def addEventListener(self, eventType: Literal['activeaudioinputchange'], listener: Callable[["ActiveAudioInputChangeEvent"], Any], once: bool = False, seq: bool = True) -> None: ...
    @overload
    def addEventListener(self, eventType: Literal['layoutchange'], listener: Callable[["LayoutChangeEvent"], Any], once: bool = False, seq: bool = True) -> None: ...
    def addEventListener(self, eventType: str, listener: Callable[[Any], Any], once: bool = False, seq: bool = True) -> None:
        return super().addEventListener(eventType, listener, once=once, seq=seq)
    
def convertToPublicationSettings(mediaInfo: MediaInfo | None):
    if mediaInfo is None:
        return PublicationSettings()
    audio: list[AudioPublicationSettings] = []
    video: list[VideoPublicationSettings] = []
    for track in mediaInfo['tracks']:
        if track['type'] == 'audio':
            format = track.get('format')
            if format:
                audio_codec = AudioCodecParameters(
                    name=cast(AudioCodec, format.get('codec')),
                    channelCount=format.get('channelNum'),
                    clockRate=format.get('sampleRate'),
                )
            else:
                audio_codec = None
            audio_publication_settings = AudioPublicationSettings(codec=audio_codec)
            audio_publication_settings._trackId = track.get('id')
            audio.append(audio_publication_settings)
        elif track['type'] == 'video':
            format = track.get('format')
            if format:
                video_codec = VideoCodecParameters(
                    name=cast(VideoCodec, format.get('codec')),
                    profile=format.get('profile'),
                )
            else:
                video_codec = None
            parameters = track.get('parameters')
            if parameters:
                r = parameters.get('resolution')
                resolution = Resolution(width=r['width'], height=r['height']) if r else None
                framerate = parameters.get('framerate')
                bitrate = parameters.get('bitrate')
                keyFrameInterval = parameters.get('keyFrameInterval')
            else:
                resolution = None
                framerate = None
                bitrate = None
                keyFrameInterval = None
            rid = track.get('rid')
                
            video_publication_settings = VideoPublicationSettings(
                codec=video_codec,
                resolution=resolution,
                frameRate=framerate,
                bitrate=bitrate,
                keyFrameInterval=keyFrameInterval,
                rid=rid,
            )
            video_publication_settings._trackId = track.get('id')
            video.append(video_publication_settings)
    return PublicationSettings(audio=audio, video=video)

def extractBitrateMultiplier(input: float | str) -> float:
    """
    Extract bitrate multiplier from a string like "x0.2".

    Args:
        input (float | str): bitrate multiplier
    Return:
        The float number after "x".
    """
    if isinstance(input, str):
        if not input.startswith('x'):
            logger.warning(f'Invalid bitrate multiplier input: {input}')
            return 0
        return float(input[1:])
    else:
        return input

def convertToSubscriptionCapabilities(mediaInfo: MediaInfo | None):
    if mediaInfo is None:
        return SubscriptionCapabilities()
    audio = None
    video = None
    for track in mediaInfo['tracks']:
        if track['type'] == 'audio':
            audio_codecs: list[AudioCodecParameters] = []
            optional = track.get('optional')
            if optional:
                format = optional.get('format')
                if format:
                    for audio_codec_info in format:
                        audio_codec = AudioCodecParameters(
                            name=cast(AudioCodec, audio_codec_info.get('codec')),
                            channelCount=audio_codec_info.get('channelNum'),
                            clockRate=audio_codec_info.get('sampleRate'),
                        )
                        audio_codecs.append(audio_codec)
            audio_codecs.sort(key=lambda p: p.name)
            audio = AudioSubscriptionCapabilities(codecs=audio_codecs)
        elif track['type'] == 'video':
            video_codecs: list[VideoCodecParameters] = []
            resolutions: list[Resolution] = []
            bitrates: list[float] = []
            frameRates: list[float] = []
            keyFrameIntervals: list[float] = []
            optional = track.get('optional')
            if optional:
                format = optional.get('format')
                if format:
                    for video_codec_info in format:
                        video_codec = VideoCodecParameters(
                            name=cast(VideoCodec, video_codec_info.get('codec')),
                            profile=video_codec_info.get('profile'),
                        )
                        video_codecs.append(video_codec)
                parameters = optional.get('parameters')
                if parameters:
                    resolutions = [Resolution(width=r['width'], height=r['height']) for r in parameters.get('resolution') or []]
                    resolutions.sort(key=lambda r: (r.width, r.height))
                    bitrates = [extractBitrateMultiplier(b) for b in parameters.get('bitrate') or []]
                    if not bitrates or bitrates[-1] != 1.0:
                        bitrates.append(1.0)
                    bitrates.sort()
                    frameRates = [r for r in parameters.get('framerate') or []]
                    frameRates.sort()
                    keyFrameIntervals = [i for i in parameters.get('keyFrameInterval') or []]
                    keyFrameIntervals.sort()
            video_codecs.sort(key=lambda p: p.name)
            video = VideoSubscriptionCapabilities(
                codecs=video_codecs,
                resolutions=resolutions,
                frameRates=frameRates,
                bitrateMultipliers=bitrates,
                keyFrameIntervals=keyFrameIntervals,
            )
    return SubscriptionCapabilities(audio=audio, video=video)
    
@dataclass
class RemoteMixedStream(RemoteStream):
    
    def __init__(self, info: StreamInfo):
        assert info['type'] == 'mixed'
        super().__init__(id=info.get('id'), tracks=[], quic_stream=None, source=StreamSourceInfo(audio='mixed', video='mixed'))
        self.settings = convertToPublicationSettings(info.get('media'))
        self.extraCapabilities = convertToSubscriptionCapabilities(info.get('media'))
     
class StreamEvent(OwtEvent):
    stream: RemoteStream
    
    def __init__(self, type: str, stream: RemoteStream) -> None:
        super().__init__(type)
        self.stream = stream
        
class ActiveAudioInputChangeEvent(OwtEvent):
    activeAudioInputStreamId: str
    
    def __init__(self, type: str, activeAudioInputStreamId: str) -> None:
        super().__init__(type)
        self.activeAudioInputStreamId = activeAudioInputStreamId
        
class LayoutChangeEvent(OwtEvent):
    layout: dict
    
    def __init__(self, type: str, layout) -> None:
        super().__init__(type)
        self.layout = layout