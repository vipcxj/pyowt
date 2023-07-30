from dataclasses import dataclass
from typing import Literal

from .portal_types import Resolution as SigResolution

TrackKink = Literal['audio'] | Literal['video'] | Literal['av']

@dataclass
class Resolution:
    width: int
    height: int
    
    def to_dict(self) -> SigResolution:
        return {
            'width': self.width,
            'height': self.height,
        }