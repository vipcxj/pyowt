import sys

if sys.version_info < (3, 11):
    from typing_extensions import (Any, Literal, NotRequired, TypeAlias,
                                   TypedDict, overload)
else:
    from typing import TypedDict, NotRequired, Any, Literal, overload, TypeAlias

from .constants import (AudioCodec, AudioSourceInfo, TransportType, VideoCodec,
                        VideoProfile, VideoSourceInfo)


class ReconnectionOptions(TypedDict):
    keepTime: float
    """
    -1: Use server side configured 'reconnection_timeout' value; Others: specified keepTime in seconds
    """

class Sdk(TypedDict):
    type: str
    version: str

class ClientInfo(TypedDict):
    sdk: Sdk

class LoginInfo(TypedDict):
    token: str
    userAgent: ClientInfo
    reconnection: NotRequired[ReconnectionOptions | Literal[False] | None]
    protocol: NotRequired[str | None]
    
class PendingMessage(TypedDict):
    event: str
    data: Any
    seq: int
    """
    Internal Seq No
    """
    
class ReloginResponse(TypedDict):
    ticket: str
    """
    base64 encoded ReconnectionTicket object
    """
    messages: list[PendingMessage]
    
class PermissionData(TypedDict):
    audio: bool
    video: bool
    data: bool
    
class Permission(TypedDict):
    publish: PermissionData
    subscribe: PermissionData
    
class ParticipantInfo(TypedDict):
    id: str
    """
    The ID of the participant. It varies when a single user join different conferences.
    """
    role: str
    user: str
    """
    The user ID of the participant. It can be integrated into existing account management system.
    """
    
class ParticipantJoinAction(TypedDict):
    action: Literal['join']
    data: ParticipantInfo
    
class ParticipantLeaveAction(TypedDict):
    action: Literal['leave']
    data: str
    """
    Participant Id
    """

ParticipantAction: TypeAlias = ParticipantJoinAction | ParticipantLeaveAction
    
class PublicationInfo(TypedDict):
    owner: str
    
class UnpublicationRequest(TypedDict):
    id: str
    """
    same as the stream id.
    """
    
class UnsubscriptionRequest(TypedDict):
    id: str
    """
    Subscription Id
    """
    
class StreamControlInfo(TypedDict):
    id: str
    """
    Stream id, Must refer to a forward stream
    """
    operation: Literal['pause', "play"]
    data: Literal['audio', 'video', 'av']
    """
    If operation equals "pause" or "play"
    """
    
class VideoParametersSpecification(TypedDict):
    resolution: NotRequired["Resolution | None"]
    framerate: NotRequired[float | None]
    bitrate: NotRequired[float | str | None]
    keyFrameInterval: NotRequired[float | None]
    
class AudioUpdate(TypedDict('AudioUpdate', {
    'from': str
})):
    pass
    
class VideoUpdate(TypedDict('VideoUpdate', {
    'from': NotRequired[str | None]
})):
    parameters: NotRequired[VideoParametersSpecification | None]
    
class SubscriptionUpdate(TypedDict):
    audio: NotRequired[AudioUpdate | None]
    video: NotRequired[VideoUpdate | None]
    
class SubscriptionControlInfo(TypedDict):
    id: str
    operation: Literal['update', 'pause', 'play', 'querybwe']
    data: NotRequired[SubscriptionUpdate | Literal['audio', 'video', 'av'] | None]
    """
    SubscriptionUpdate If operation equals "update",
    ("audio" | "video" | "av") If operation equals "pause" or "play",
    undefined If operation equals "querybwe".
    """
    
class BWEResult(TypedDict):
    estimatedBitrate: float
    """
    Send side estimated bitrate if enabled
    """
    

class AugmentInfo(TypedDict):
    pass

class ViewInfo(TypedDict):
    pass

class BaseStreamInfo(TypedDict):
    id: str
    media: NotRequired["MediaInfo | None"]
    data: bool

class ForwardStreamInfo(BaseStreamInfo):
    type: Literal['forward']
    info: PublicationInfo
    
class AugmentedStreamInfo(BaseStreamInfo):
    type: Literal['augmented']
    info: AugmentInfo
    
class MixedStreamInfo(BaseStreamInfo):
    type: Literal['mixed']
    info: ViewInfo
    
StreamInfo: TypeAlias = ForwardStreamInfo | AugmentedStreamInfo | MixedStreamInfo
    
class RoomInfo(TypedDict):
    id: str
    views: NotRequired[list[str] | None]
    streams: NotRequired[list[StreamInfo] | None]
    participants: NotRequired[list[ParticipantInfo] | None]
    
class LoginResult(TypedDict):
    id: str
    user: str
    role: str
    permission: Permission
    room: RoomInfo
    reconnectionTicket: NotRequired[str | None]
    webTransportToken: NotRequired[str | None]
    """
    Base64Encoded(object(ReconnectionTicket))) when reconnection is promised
    """

class ReconnectionTicket(TypedDict):
    participantId: str
    ticketId: str
    notBefore: float
    notAfter: float
    signature: str
    
class TextSendMessage(TypedDict):
    to: str
    """
    Participant Id or all
    """
    message: str
    """
    The message length must not greater than 2048
    """
    
class TextReceiveMessage(TypedDict('TextReceiveMessage', {
    'from': str, # Participant Id
})):
    to: Literal['all', 'me']
    message: str

class OfferAnswer(TypedDict):
    type: Literal['offer', 'answer']
    sdp: NotRequired[str | None]
    
class Candidate(TypedDict):
    sdpMid: str
    sdpMLineIndex: int
    candidate: str

class RemovedCandidate(TypedDict):
    sdpMid: NotRequired[str | None]
    sdpMLineIndex: NotRequired[int | None]
    candidate: str
    
class CandidateMessage(TypedDict):
    type: Literal['candidate']
    candidate: Candidate
    
class RemovedCandidatesMessage(TypedDict):
    type: Literal['removed-candidates']
    candidates: list[RemovedCandidate]
    
class SOACMessage(TypedDict):
    id: str
    """
    Transport ID returned in publishing or subscribing
    """
    signaling: OfferAnswer | CandidateMessage | RemovedCandidatesMessage
    
class RTCRtpSendParameters(TypedDict):
    pass

class RtpConfig(TypedDict):
    ssrc: str
    sendParameters: RTCRtpSendParameters # [RTCRtpSendParameters](https://www.w3.org/TR/webrtc/#rtcsendrtpparameters) defined in WebRTC 1.0.

class SoacTransportProgress(TypedDict):
    id: str # transportId
    status: Literal['soac']
    data: OfferAnswer | CandidateMessage
    
class ReadyTransportProgress(TypedDict):
    id: str # transportId
    sessionId: NotRequired[str | None]
    status: Literal['ready']
    
class ErrorTransportProgress(TypedDict):
    id: str # transportId
    status: Literal['error']
    sessionId: NotRequired[str | None]
    data: str # reason
    
class RtpTransportProgress(TypedDict):
    id: str # transportId
    status: Literal['rtp']
    data: RtpConfig
    
TransportProgress = SoacTransportProgress | ReadyTransportProgress | ErrorTransportProgress | RtpTransportProgress

class SessionProgress(TypedDict):
    id: str # StreamId returned in publishing or SubscriptionId returned in subscribing
    status: Literal['ready'] | Literal['error']

class AudioFormat(TypedDict):
    codec: NotRequired[AudioCodec]
    sampleRate: NotRequired[float]
    channelNum: NotRequired[int]
    
class VideoFormat(TypedDict):
    codec: NotRequired[VideoCodec]
    profile: NotRequired[VideoProfile]
    """
    If codec does NOT equal "h264", should be None
    """
    
class Resolution(TypedDict):
    width: int
    height: int
    
class VideoParameters(TypedDict):
    resolution: NotRequired[Resolution | None]
    framerate: NotRequired[float | None]
    bitrate: NotRequired[float | str | None]
    keyFrameInterval: NotRequired[float | None]
    
class TrackInfoOptionalParameters(TypedDict):
    resolution: NotRequired[list[Resolution] | None]
    framerate: NotRequired[list[float] | None]
    bitrate: NotRequired[list[float | str] | None]
    keyFrameInterval: NotRequired[list[float] | None]
    
class TrackInfoOptional(TypedDict):
    format: NotRequired[list[AudioFormat] | list[VideoFormat] | None]
    parameters: NotRequired[TrackInfoOptionalParameters | None]

class TrackInfo(TypedDict):
    id: NotRequired[str | None]
    type: Literal['audio', 'video']
    format: NotRequired[AudioFormat | VideoFormat | None]
    parameters: NotRequired[VideoParameters | None]
    status: NotRequired[Literal['active', 'inactive'] | None]
    source: NotRequired[AudioSourceInfo | VideoSourceInfo | None]
    mid: NotRequired[str | None]
    """
    undefined if transport's type is "quic"
    """
    rid: NotRequired[str | None]
    optional: NotRequired[TrackInfoOptional | None]
    

class MediaInfo(TypedDict):
    tracks: list[TrackInfo]
    
class TransportOptions(TypedDict):
    type: TransportType
    id: NotRequired[str | None]
    """
    null will result to create a new transport channel. Always be null if transport type is webrtc because webrtc agent doesn't support multiple transceivers on a single PeerConnection at this time.
    """

class PublicationRequest(TypedDict):
    media: NotRequired[MediaInfo | None]
    data: NotRequired[bool | None]
    transport: NotRequired[TransportOptions | None]
    attributes: NotRequired[dict | None]
    
class PublicationOrSubscriptionResult(TypedDict):
    transportId: str
    """
    Can be reused in the following publication or subscription.
    """
    id: str
    """
    will be used as the stream id when it gets ready.
    """
    
class TrackSubInfo(TypedDict('TrackSubInfo', {
    'from': NotRequired[str | None],
})):
    type: Literal['audio', 'video']
    mid: NotRequired[str | None]
    parameters: NotRequired[VideoParameters | None]
    
class MediaSubOptions(TypedDict):
    tracks: list[TrackSubInfo]
    
class MediaSubscriptionRequest(TypedDict):
    media: MediaSubOptions
    transport: NotRequired[TransportOptions | None]
    
    
class QuicSubscriptionRequestData(TypedDict('QuicSubscriptionRequestData', { "from": str })):
    pass
class QuicSubscriptionRequest(TypedDict):
    data: QuicSubscriptionRequestData
    transport: NotRequired[TransportOptions | None]
    
SubscriptionRequest: TypeAlias = MediaSubscriptionRequest | QuicSubscriptionRequest
    
class BaseStreamUpdateMessage(TypedDict):
    id: str
    """
    stream id
    """

class StreamAddUpdateMessage(BaseStreamUpdateMessage):
    status: Literal['add']
    data: StreamInfo
    
class MixedVideoLayout(TypedDict):
    pass
    
class StreamUpdateStatus(TypedDict):
    field: Literal['audio.status', 'video.status']
    value: Literal['active', 'inactive']
    
class StreamUpdateVideoLayout(TypedDict):
    field: Literal['video.layout']
    value: MixedVideoLayout
    
class StreamUpdateActiveInput(TypedDict):
    field: Literal['activeInput']
    value: str
    
class StreamUpdateWhole(TypedDict):
    field: Literal['.']
    value: StreamInfo
    
StreamUpdate: TypeAlias = StreamUpdateStatus | StreamUpdateVideoLayout | StreamUpdateActiveInput | StreamUpdateWhole

class StreamUpdateUpdateMessage(BaseStreamUpdateMessage):
    status: Literal['update']
    data: StreamUpdate
    
class StreamRemoveUpdateMessage(BaseStreamUpdateMessage):
    status: Literal['remove']
    
StreamUpdateMessage = StreamAddUpdateMessage | StreamUpdateUpdateMessage | StreamRemoveUpdateMessage