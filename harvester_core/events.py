"""Tiny structured reporting contract shared by CLI and future frontends."""
from dataclasses import dataclass, field
from typing import Any, Callable

@dataclass(frozen=True)
class Event:
    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

Reporter = Callable[[Event], None]

def emit(reporter, kind, message, **data):
    if reporter:
        reporter(Event(kind, message, data))
