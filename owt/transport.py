from dataclasses import dataclass
from typing import Literal

from .common import Transceiver

TransportType = Literal['quic', 'webrtc']

@dataclass
class TransportConstraints:
    type: TransportType
    """
    Transport type for publication and subscription.
    """
    id: str | None = None
    """
    Transport ID. Undefined transport ID results server to assign a new
    one. It should always be undefined if transport type is webrtc since the
    webrtc agent of OWT server doesn't support multiple transceivers on a
    single PeerConnection.
    """

@dataclass
class TransportSettings:
    type: TransportType
    id: str
    rtpTransceivers: list[Transceiver]