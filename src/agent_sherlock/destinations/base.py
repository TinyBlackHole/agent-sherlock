from __future__ import annotations

from typing import Protocol


class MessageDestination(Protocol):
    """The single configured delivery channel."""

    @property
    def name(self) -> str: ...

    def send(self, text: str) -> None: ...
