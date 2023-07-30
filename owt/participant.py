from typing import Any, Callable, Literal, overload

from .common import EventDispatcher, OwtEvent


class Participant(EventDispatcher):
    id: str
    """
    The ID of the participant. It varies when a single user join different conferences.
    """
    role: str
    userId: str
    """
    The user ID of the participant. It can be integrated into existing account management system.
    """
    def __init__(self, id: str, role: str, userId: str) -> None:
        super().__init__()
        self.id = id
        self.role = role
        self.userId = userId

    @overload
    def addEventListener(self, eventType: Literal['left'], listener: Callable[[OwtEvent], Any], once: bool = False, seq: bool = True) -> None: ...
    @overload
    def addEventListener(self, eventType: str, listener: Callable[[OwtEvent], Any], once: bool = False, seq: bool = True) -> None: ...
    def addEventListener(self, eventType: str, listener: Callable[[OwtEvent], Any], once: bool = False, seq: bool = True) -> None:
        return super().addEventListener(eventType, listener, once=once, seq=seq)