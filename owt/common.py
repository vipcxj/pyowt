import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypeAlias, overload

from aiortc import RTCRtpTransceiver

from .codec import AudioEncodingParameters, VideoEncodingParameters
from .constants import AudioSourceInfo, VideoSourceInfo
from .mediaformat import Resolution, TrackKink
from .portal_types import VideoParameters as SigVideoParameters
from .sub_types import (AudioSubscriptionConstraints,
                        VideoSubscriptionConstraints)
from .webrtc import RTCRtpEncodingParameters
from .utils import ensure_future


class ConferenceError(Exception):
    message: str
    
    def __init__(self, message: str, *args: object) -> None:
        self.message = message
        super().__init__(self.message, *args)

class OwtEvent:
    type: str
    
    def __init__(self, type: str) -> None:
        self.type = type
        
class ErrorEvent(OwtEvent):
    error: Exception
    
    def __init__(self, type: str, error: Exception) -> None:
        super().__init__(type)
        self.error = error
        
class MessageEvent(OwtEvent):
    
    def __init__(self, type: str, message: Any, origin: str | None = None, to: str | None = None) -> None:
        super().__init__(type)
        self.message = message
        self.origin = origin
        self.to = to
        
class MuteEvent(OwtEvent):
    kind: TrackKink
    
    def __init__(self, type: str, kind: TrackKink) -> None:
        super().__init__(type)
        self.kind = kind
    

Listener = Callable[[OwtEvent], Any]

@dataclass
class ListenerBox:
    listener: Listener
    once: bool
    seq: bool
    
class CompoundError(ConferenceError):
    errors: list[BaseException]
    
    def __init__(self, errors: list[BaseException]) -> None:
        super().__init__('|'.join((f'{e}' for e in errors)))
        self.errors = errors

class EventDispatcher:
    
    listeners_map: dict[str, list[ListenerBox]]
    lock: asyncio.Lock
    
    def __init__(self) -> None:
        self.listeners_map = {}
        self.lock = asyncio.Lock()
    
    @overload
    def addEventListener(self, eventType: Literal['data'], listener: Callable[[MessageEvent], Any], once: bool = False, seq: bool = True) -> None: ...
    @overload
    def addEventListener(self, eventType: Literal['id'], listener: Callable[[MessageEvent], Any], once: bool = False, seq: bool = True) -> None: ...
    @overload
    def addEventListener(self, eventType: Literal['ended'], listener: Callable[[OwtEvent], Any], once: bool = False, seq: bool = True) -> None: ...
    @overload
    def addEventListener(self, eventType: str, listener: Listener, once: bool = False, seq: bool = True) -> None: ...
    def addEventListener(self, eventType: str, listener: Any, once: bool = False, seq: bool = True) -> None:
        if eventType not in self.listeners_map:
            self.listeners_map[eventType] = []
        self.listeners_map[eventType].append(ListenerBox(listener=listener, once=once, seq=seq))
    
    def removeEventListener(self, eventType: str, listener: Any) -> None:
        if eventType in self.listeners_map:
            boxes = self.listeners_map[eventType]
            box = next((box for box in boxes if box.listener == listener), None)
            if box:
                boxes.remove(box)
                if len(boxes) == 0:
                    del self.listeners_map[eventType]
    
    def clearEventListener(self, eventType: str) -> None:
        if eventType in self.listeners_map:
            del self.listeners_map[eventType]
    
    async def dispatchEventAwaitable(self, event: OwtEvent):
        async with self.lock:
            boxes = self.listeners_map.get(event.type, [])
            if len(boxes) == 0:
                return
            pal_listeners: list[Listener] = []
            seq_listeners: list[Listener] = []
            for box in boxes:
                if box.seq:
                    seq_listeners.append(box.listener)
                else:
                    pal_listeners.append(box.listener)
            async def invoke_pal_listeners():
                results = await asyncio.gather(*[ensure_future(l, event) for l in pal_listeners], return_exceptions=True)
                excs = [e for e in results if isinstance(e, BaseException)]
                if len(excs) > 0:
                    if len(excs) > 1:
                        raise CompoundError(excs)
                    else:
                        raise excs[0]
            async def invoke_seq_listeners():
                for l in seq_listeners:
                    await ensure_future(l, event)
            results = await asyncio.gather(invoke_pal_listeners(), invoke_seq_listeners(), return_exceptions=True)
            boxes = [box for box in boxes if not box.once]
            self.listeners_map[event.type] = boxes
            errors: list[BaseException] = []
            for res in results:
                if isinstance(res, BaseException):
                    if isinstance(res, CompoundError):
                        errors.extend(res.errors)
                    else:
                        errors.append(res)
            if len(errors) > 0:
                if len(errors) > 1:
                    raise CompoundError(errors)
                else:
                    raise errors[0]
            
    def dispatchEvent(self, event: OwtEvent, loop: asyncio.AbstractEventLoop | None = None):
        asyncio.ensure_future(self.dispatchEventAwaitable(event), loop=loop)
        
                

@dataclass
class AudioTransceiver:
    type: Literal['audio']
    transceiver: RTCRtpTransceiver
    option: list[AudioEncodingParameters] | list[RTCRtpEncodingParameters] | AudioSubscriptionConstraints | VideoSubscriptionConstraints | bool
    source: AudioSourceInfo | None = None
    from_id: str | None = None
    parameters: None = None
    
@dataclass
class VideoParameters:
    resolution: Resolution | None = None
    framerate: float | None = None
    bitrate: str | float | None = None
    keyFrameInterval: float | None = None
    
    def to_dict(self) -> SigVideoParameters:
        return {
            'resolution': self.resolution.to_dict() if self.resolution else None,
            'framerate': self.framerate,
            'bitrate': self.bitrate,
            'keyFrameInterval': self.keyFrameInterval,
        }
    
@dataclass
class VideoTransceiver:
    type: Literal['video']
    transceiver: RTCRtpTransceiver
    option: list[VideoEncodingParameters] | list[RTCRtpEncodingParameters] | AudioSubscriptionConstraints | VideoSubscriptionConstraints | bool
    source: VideoSourceInfo | None = None
    from_id: str | None = None
    parameters: VideoParameters | None = None
    
Transceiver: TypeAlias = AudioTransceiver | VideoTransceiver