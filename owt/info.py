from dataclasses import dataclass

from .participant import Participant
from .stream import RemoteStream


@dataclass
class ConferenceInfo:
    id: str
    participants: list[Participant]
    remoteStreams: list[RemoteStream]
    self: Participant