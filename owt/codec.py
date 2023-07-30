from dataclasses import dataclass

from .constants import AudioCodec, VideoCodec, VideoProfile


@dataclass
class AudioCodecParameters:
    """
    Codec parameters for an audio track.
    """
    name: AudioCodec
    """
    Name of a codec. Please a value in AudioCodec. However,
    some functions do not support all the values in AudioCodec.
    """
    channelCount: int | None = None
    """
    Numbers of channels for an audio track.
    """
    clockRate: float | None = None
    """
    The codec clock rate expressed in Hertz.
    """
    

@dataclass
class AudioEncodingParameters:
    """
    Encoding parameters for sending an audio track.
    """
    codec: AudioCodecParameters | None = None
    maxBitrate: float | None = None

@dataclass
class VideoCodecParameters:
    """
    Codec parameters for a video track.
    """
    name: VideoCodec
    """
    Name of a codec.Please a value in VideoCodec.However,
    some functions do not support all the values in VideoCodec.
    """
    profile: VideoProfile | None = None
    """
    The profile of a codec. Profile may not apply to all codecs.
    If codec not equals "h264", should be None
    """

@dataclass
class VideoEncodingParameters:
    """
    Encoding parameters for sending a video track.
    """
    
    codec: VideoCodecParameters | None = None
    maxBitrate: float | None = None
    """
    Max bitrate expressed in kbps.
    """