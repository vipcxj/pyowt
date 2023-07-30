from dataclasses import dataclass, field

from .codec import AudioCodecParameters, VideoCodecParameters
from .mediaformat import Resolution

@dataclass
class AudioSubscriptionCapabilities:
    codecs: list[AudioCodecParameters] = field(default_factory=list)
    
@dataclass
class VideoSubscriptionCapabilities:
    codecs: list[VideoCodecParameters] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    frameRates: list[float] = field(default_factory=list)
    bitrateMultipliers: list[float] = field(default_factory=list)
    keyFrameIntervals: list[float] = field(default_factory=list)
    
@dataclass
class SubscriptionCapabilities:
    audio: AudioSubscriptionCapabilities | None = None
    video: VideoSubscriptionCapabilities | None = None
    
@dataclass
class AudioSubscriptionConstraints:
    codecs: list[AudioCodecParameters] | None = None
    
@dataclass
class VideoSubscriptionConstraints:
    codecs: list[VideoCodecParameters] | None = None
    resolution: Resolution | None = None
    frameRate: float | None = None
    bitrateMultiplier: float | None = None
    keyFrameInterval: float | None = None
    rid: str | None = None